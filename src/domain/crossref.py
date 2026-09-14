"""Crossref: сборка депозита метаданных и правила, по которым он собирается.

Что здесь происходит. Префикс, выданный Crossref, сам по себе ничего не даёт:
`doi.org/10.XXXXX/ruz.632` отвечает 404 до тех пор, пока метаданные статьи не
отправлены в Crossref депозитом. Депозит — это XML по схеме crossref 5.3.1,
который уезжает на сервлет (`src/infrastructure/external/crossref.py`), а
состояние отправки живёт в `crossref_deposits`.

Три решения, которые здесь зашиты и которые дороже всего менять потом:

* **Суффикс DOI считается от `articles.id`**, а не от слага. Слаг меняется при
  правке заголовка, а DOI неизменен по определению — суффикс от слага означал
  бы, что через год половина ссылок ведёт не туда либо что мы выдали два DOI
  одной статье.
* **Resource URL — `/id/<article_id>`**, резолвер, а не `/uz/article/<slug>`.
  По той же причине: резолвер переживает переименование статьи, прямая ссылка
  на слаг — нет. Резолвер отвечает 301 на каноническую локаль, это Crossref
  устраивает.
* **Депонируем только статьи журналов** (`publication_type = 'article'` при
  заполненном `issue_id`). У монографий, диссертаций и докладов конференций в
  схеме Crossref свои корневые элементы (`book`, `dissertation`, `conference`)
  со своими обязательными полями — сделать их «заодно» нельзя, а притвориться,
  что диссертация это статья журнала, значит навсегда положить в мировой индекс
  неверный тип записи.

Разбор ФИО. Crossref хочет имя и фамилию раздельно, у нас в базе — одна строка
(`article_authors.author_name`). Однозначно разобрать «Каримов Азиз Рустамович»
и «Aziz Karimov» одним правилом нельзя, поэтому:
  * запятая — явная форма и всегда главнее эвристики: «Фамилия, Имя Отчество»;
  * иначе, если есть отчество (-ович/-евич/-овна/-евна/ o'g'li / qizi) —
    фамилия первая (узбекская и русская запись);
  * иначе фамилия считается последним словом (латинская запись).
Эвристика ошибается, и это нормально ровно потому, что превью депозита
показывает разбор ДО отправки: увидел неверное — поправил `author_name` на
форму с запятой. Молча угадывать и сразу отправлять было бы хуже.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from xml.etree import ElementTree as ET

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.infrastructure.external.crossref import Credentials
from src.infrastructure.persistence.models import (
    Article,
    ArticleAuthor,
    ArticleReference,
    CrossrefDeposit,
    Issue,
    Journal,
)

CROSSREF_NS = "http://www.crossref.org/schema/5.3.1"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
JATS_NS = "http://www.ncbi.nlm.nih.gov/JATS1"
SCHEMA_VERSION = "5.3.1"
SCHEMA_LOCATION = (
    f"{CROSSREF_NS} https://www.crossref.org/schemas/crossref{SCHEMA_VERSION}.xsd"
)

ET.register_namespace("", CROSSREF_NS)
ET.register_namespace("xsi", XSI_NS)
ET.register_namespace("jats", JATS_NS)

# Что депонируем. Остальные типы публикаций — другой корневой элемент схемы.
DEPOSITABLE_TYPES = {"article"}

# Отчество/сын-дочь: маркер того, что запись начинается с фамилии.
_PATRONYMIC_RE = re.compile(
    # Кириллица и латиница разом: в базе одни и те же отчества записаны и так,
    # и так («Рустамович» / «Rustamovich»), а по одной кириллической форме
    # узбекская латиница не опознавалась бы и разбиралась как «Имя Фамилия».
    r"(ович|евич|овна|евна|ична|инична"
    r"|ovich|evich|ovna|evna|ichna|inichna"
    r"|ugli|o'g'li|o‘g‘li|oglu|qizi|kizi)$",
    re.IGNORECASE,
)
# Страницы: «12-18», «12–18», «С. 12-18», «12».
_PAGES_RANGE_RE = re.compile(r"(\d+)\s*[-–—]\s*(\d+)")
_PAGES_SINGLE_RE = re.compile(r"(\d+)")
# Авторы из свободного поля articles.authors — те же разделители, что в OAI.
_AUTHORS_SPLIT_RE = re.compile(r"\s*[;]\s*")
_ORCID_RE = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]")
# Символы, которых не бывает в XML 1.0 (в базе встречаются после импортов PDF).
_ILLEGAL_XML_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

MAX_UNSTRUCTURED_CITATION = 2000


class CrossrefError(Exception):
    """Депозит собрать нельзя: не хватает данных или тип не тот."""


# --------------------------------------------------------------- нормализация

def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return _ILLEGAL_XML_RE.sub("", value).strip()


def normalize_prefix(raw: str | None) -> str:
    """`10.63212`, `10.63212/`, `https://doi.org/10.63212` → `10.63212`."""
    if not raw:
        return ""
    value = raw.strip().rstrip("/")
    value = re.sub(r"^https?://(dx\.)?doi\.org/", "", value, flags=re.IGNORECASE)
    return value


def journal_prefix(journal: Journal | None) -> str:
    """Префикс журнала. У каждого издателя он свой, платформенный — запасной."""
    meta = (journal.meta if journal is not None else None) or {}
    return normalize_prefix(meta.get("doi_prefix")) or normalize_prefix(
        settings.CROSSREF_DEFAULT_PREFIX
    )


def resolve_credentials(journal: Journal | None) -> Credentials:
    """Под какой учёткой уходит депозит этого журнала.

    Префикс принадлежит членству, а не платформе: журнал, у которого своё
    членство в Crossref, обязан ходить под своей учёткой — нашим логином его
    префикс не задепонировать. Ключ учётки лежит в
    `journals.metadata.crossref_account`, сама учётка — в env (в БД её держать
    нельзя, journals.metadata читается публично).
    """
    meta = (journal.meta if journal is not None else None) or {}
    key = (meta.get("crossref_account") or "").strip()
    if key:
        account = settings.crossref_accounts.get(key) or {}
        login = (account.get("login") or "").strip()
        password = (account.get("password") or "").strip()
        if not login or not password:
            raise CrossrefError(
                f"Журнал ходит под учёткой «{key}», но её нет в CROSSREF_ACCOUNTS"
            )
        return Credentials(login=login, password=password, account=key)

    login = (settings.CROSSREF_LOGIN_ID or "").strip()
    password = (settings.CROSSREF_LOGIN_PASSWORD or "").strip()
    if not login or not password:
        raise CrossrefError(
            "Не заданы CROSSREF_LOGIN_ID / CROSSREF_LOGIN_PASSWORD — "
            "депозит выключен"
        )
    return Credentials(login=login, password=password)


def compute_doi(article: Article, journal: Journal | None) -> str:
    prefix = journal_prefix(journal)
    if not prefix:
        raise CrossrefError(
            "У журнала не задан DOI-префикс (journals.metadata.doi_prefix) "
            "и нет CROSSREF_DEFAULT_PREFIX"
        )
    suffix = settings.CROSSREF_DOI_SUFFIX_TEMPLATE.format(
        article_id=article.id,
        issue_id=article.issue_id or 0,
        year=(article.data.year if article.data else datetime.now(timezone.utc).year),
    )
    return f"{prefix}/{suffix}"


def split_person_name(raw: str) -> tuple[str, str]:
    """`(given_name, surname)`. Правила — в докстринге модуля."""
    name = clean_text(raw)
    if not name:
        return "", ""

    if "," in name:
        surname, _, given = name.partition(",")
        return " ".join(given.split()), " ".join(surname.split())

    parts = name.split()
    if len(parts) == 1:
        # Одно слово — фамилия: given_name в схеме необязателен, surname нет.
        return "", parts[0]

    if any(_PATRONYMIC_RE.search(p) for p in parts[1:]):
        return " ".join(parts[1:]), parts[0]

    return " ".join(parts[:-1]), parts[-1]


def parse_pages(raw: str | None) -> tuple[str, str]:
    """`(first_page, last_page)`; пусто, если чисел в строке нет."""
    text = clean_text(raw)
    if not text:
        return "", ""
    match = _PAGES_RANGE_RE.search(text)
    if match:
        return match.group(1), match.group(2)
    single = _PAGES_SINGLE_RE.search(text)
    return (single.group(1), "") if single else ("", "")


def normalize_orcid(raw: str | None) -> str:
    """ORCID в форме, которую требует схема: https://orcid.org/0000-...."""
    if not raw:
        return ""
    match = _ORCID_RE.search(raw.strip().upper())
    return f"https://orcid.org/{match.group(0)}" if match else ""


def _site(path: str) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}{path}"


def resource_url(article: Article) -> str:
    # Резолвер, а не слаг: см. докстринг модуля.
    return _site(f"/id/{article.id}")


def pdf_url(article: Article) -> str:
    return _site(f"/pdf/{article.slug}.pdf") if article.slug else ""


# --------------------------------------------------------------- материал

@dataclass
class Contributor:
    given_name: str
    surname: str
    orcid: str = ""


@dataclass
class Citation:
    key: str
    doi: str = ""
    unstructured: str = ""


@dataclass
class DepositItem:
    """Всё, что нужно для одной записи `journal_article`, уже разобранное."""

    article: Article
    issue: Issue
    journal: Journal
    doi: str
    contributors: list[Contributor] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)

    @property
    def publication_date(self) -> date:
        return self.article.data or date(self.issue.year or date.today().year, 1, 1)

    def problems(self) -> list[str]:
        """Чего не хватает для валидного депозита. Пусто = можно отправлять."""
        out: list[str] = []
        if not clean_text(self.journal.name):
            out.append("у журнала нет названия")
        if not clean_text(self.journal.issn) and not clean_text(self.journal.printed_issn):
            # Схема ISSN не требует, бизнес-правила Crossref — требуют: журнал
            # без ISSN он не примет, и батч вернётся ошибкой.
            out.append("у журнала не заполнен ни один ISSN")
        if not clean_text(self.article.title):
            out.append("у статьи нет заголовка")
        if not self.contributors:
            out.append("у статьи не указаны авторы")
        if not (self.article.data or self.issue.year):
            out.append("нет даты публикации: пусты и articles.data, и issues.year")
        return out


# --------------------------------------------------------------- сборка XML

def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrs) -> ET.Element:
    el = ET.SubElement(parent, f"{{{CROSSREF_NS}}}{tag}")
    if text:
        el.text = text
    for key, value in attrs.items():
        el.set(key, value)
    return el


def _date_element(parent: ET.Element, tag: str, value: date, media_type: str) -> None:
    """`month?, day?, year` — порядок из схемы, год ПОСЛЕДНИЙ."""
    el = ET.SubElement(parent, f"{{{CROSSREF_NS}}}{tag}")
    el.set("media_type", media_type)
    _sub(el, "month", f"{value.month:02d}")
    _sub(el, "day", f"{value.day:02d}")
    _sub(el, "year", str(value.year))


def build_batch_id(article_id: int, stamp: datetime) -> str:
    return f"ruz-{article_id}-{stamp.strftime('%Y%m%d%H%M%S')}"


def build_timestamp(stamp: datetime) -> str:
    """Версия записи: чем больше число, тем свежее. Crossref не примет
    повторный депозит с timestamp меньше предыдущего."""
    return stamp.strftime("%Y%m%d%H%M%S%f")[:17]


def build_deposit_xml(item: DepositItem, *, batch_id: str, stamp: datetime) -> bytes:
    """XML одного депозита. Порядок элементов — строго по crossref 5.3.1."""
    root = ET.Element(f"{{{CROSSREF_NS}}}doi_batch")
    root.set("version", SCHEMA_VERSION)
    root.set(f"{{{XSI_NS}}}schemaLocation", SCHEMA_LOCATION)

    head = _sub(root, "head")
    _sub(head, "doi_batch_id", batch_id)
    _sub(head, "timestamp", build_timestamp(stamp))
    depositor = _sub(head, "depositor")
    _sub(depositor, "depositor_name", settings.CROSSREF_DEPOSITOR_NAME)
    _sub(depositor, "email_address", settings.CROSSREF_DEPOSITOR_EMAIL)
    _sub(head, "registrant", settings.CROSSREF_REGISTRANT)

    body = _sub(root, "body")
    journal = _sub(body, "journal")

    meta = _sub(journal, "journal_metadata")
    _sub(meta, "full_title", clean_text(item.journal.name))
    if clean_text(item.journal.issn):
        _sub(meta, "issn", clean_text(item.journal.issn), media_type="electronic")
    if clean_text(item.journal.printed_issn):
        _sub(meta, "issn", clean_text(item.journal.printed_issn), media_type="print")

    pub_date = item.publication_date
    issue_el = _sub(journal, "journal_issue")
    _date_element(issue_el, "publication_date", pub_date, "online")
    if clean_text(item.issue.volume):
        volume = _sub(issue_el, "journal_volume")
        _sub(volume, "volume", clean_text(item.issue.volume))
    if clean_text(item.issue.issue):
        _sub(issue_el, "issue", clean_text(item.issue.issue))

    article = _sub(journal, "journal_article", publication_type="full_text")

    titles = _sub(article, "titles")
    _sub(titles, "title", clean_text(item.article.title))

    if item.contributors:
        contributors = _sub(article, "contributors")
        for index, person in enumerate(item.contributors):
            el = _sub(
                contributors,
                "person_name",
                sequence="first" if index == 0 else "additional",
                contributor_role="author",
            )
            if person.given_name:
                _sub(el, "given_name", person.given_name)
            _sub(el, "surname", person.surname)
            if person.orcid:
                _sub(el, "ORCID", person.orcid)

    abstract_text = clean_text(item.article.annotation)
    if abstract_text:
        # jats:abstract — из импортированной схемы JATS, не из crossref-ного
        # пространства имён, поэтому собирается вручную.
        abstract = ET.SubElement(article, f"{{{JATS_NS}}}abstract")
        paragraph = ET.SubElement(abstract, f"{{{JATS_NS}}}p")
        paragraph.text = abstract_text

    _date_element(article, "publication_date", pub_date, "online")

    first_page, last_page = parse_pages(item.article.pages)
    if first_page:
        pages = _sub(article, "pages")
        _sub(pages, "first_page", first_page)
        if last_page:
            _sub(pages, "last_page", last_page)

    doi_data = _sub(article, "doi_data")
    _sub(doi_data, "doi", item.doi)
    _sub(doi_data, "resource", resource_url(item.article))
    if item.article.pdf and pdf_url(item.article):
        collection = _sub(doi_data, "collection", property="text-mining")
        collection_item = _sub(collection, "item")
        _sub(
            collection_item,
            "resource",
            pdf_url(item.article),
            mime_type="application/pdf",
        )

    if item.citations:
        citation_list = _sub(article, "citation_list")
        for citation in item.citations:
            el = _sub(citation_list, "citation", key=citation.key)
            if citation.doi:
                _sub(el, "doi", citation.doi)
            elif citation.unstructured:
                _sub(el, "unstructured_citation", citation.unstructured)

    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="utf-8", xml_declaration=False
    )


# --------------------------------------------------------------- домен

class CrossrefDomain:
    """Чтение материала для депозита и учёт его состояния."""

    async def load_item(self, db: AsyncSession, article_id: int) -> DepositItem:
        res = await db.execute(select(Article).where(Article.id == article_id))
        article = res.scalar_one_or_none()
        if article is None:
            raise CrossrefError(f"Статья {article_id} не найдена")
        if article.publication_type not in DEPOSITABLE_TYPES:
            raise CrossrefError(
                f"Тип «{article.publication_type}» не депонируется: у книг, "
                "диссертаций и докладов в схеме Crossref свои корневые элементы"
            )
        if not article.issue_id:
            raise CrossrefError("Статья не привязана к выпуску")
        if not article.published:
            raise CrossrefError("Статья не опубликована")

        res = await db.execute(select(Issue).where(Issue.id == article.issue_id))
        issue = res.scalar_one_or_none()
        if issue is None:
            raise CrossrefError("Выпуск статьи не найден")

        res = await db.execute(select(Journal).where(Journal.id == issue.journal_id))
        journal = res.scalar_one_or_none()
        if journal is None:
            raise CrossrefError("Журнал выпуска не найден")

        return DepositItem(
            article=article,
            issue=issue,
            journal=journal,
            doi=compute_doi(article, journal),
            contributors=await self._contributors(db, article),
            citations=await self._citations(db, article),
        )

    async def _contributors(
        self, db: AsyncSession, article: Article
    ) -> list[Contributor]:
        res = await db.execute(
            select(ArticleAuthor)
            .where(ArticleAuthor.article_id == article.id)
            .order_by(ArticleAuthor.author_order)
        )
        rows = res.scalars().all()
        if rows:
            out = []
            for row in rows:
                given, surname = split_person_name(row.author_name)
                if surname:
                    out.append(
                        Contributor(given, surname, normalize_orcid(row.orcid))
                    )
            return out

        # Запасной путь: свободное поле. Режем только по «;» — запятая внутри
        # имени означает «Фамилия, Имя», и делить по ней значило бы превратить
        # одного автора в двух.
        out = []
        for chunk in _AUTHORS_SPLIT_RE.split(clean_text(article.authors)):
            given, surname = split_person_name(chunk)
            if surname:
                out.append(Contributor(given, surname))
        return out

    async def _citations(self, db: AsyncSession, article: Article) -> list[Citation]:
        res = await db.execute(
            select(ArticleReference)
            .where(ArticleReference.article_id == article.id)
            .order_by(ArticleReference.position)
        )
        rows = res.scalars().all()
        if not rows:
            return []

        # Ссылка на статью нашей же платформы полезна Crossref только если у той
        # есть DOI: внутренний RUZ-идентификатор он не понимает — такая ссылка
        # уедет как неструктурированная.
        internal_ids = [r.cited_article_id for r in rows if r.cited_article_id]
        internal_dois: dict[int, str] = {}
        if internal_ids:
            res = await db.execute(
                select(Article.id, Article.doi).where(Article.id.in_(internal_ids))
            )
            internal_dois = {row.id: row.doi for row in res if row.doi}

        out: list[Citation] = []
        for index, row in enumerate(rows, start=1):
            doi = clean_text(row.cited_doi) or internal_dois.get(
                row.cited_article_id or 0, ""
            )
            raw = clean_text(row.raw)[:MAX_UNSTRUCTURED_CITATION]
            if not doi and not raw:
                continue
            out.append(Citation(key=f"ref{index}", doi=doi, unstructured=raw))
        return out

    # ------------------------------------------------------------ состояние

    async def get_deposit(
        self, db: AsyncSession, article_id: int, environment: str | None = None
    ) -> CrossrefDeposit | None:
        env = environment or settings.CROSSREF_ENV
        res = await db.execute(
            select(CrossrefDeposit).where(
                CrossrefDeposit.article_id == article_id,
                CrossrefDeposit.environment == env,
            )
        )
        return res.scalar_one_or_none()

    async def upsert_deposit(
        self, db: AsyncSession, *, item: DepositItem, created_by=None
    ) -> CrossrefDeposit:
        """Строка депозита под текущую среду. Повторная отправка переиспользует
        её: DOI у статьи один, история пишется в status/attempts."""
        deposit = await self.get_deposit(db, item.article.id)
        if deposit is None:
            deposit = CrossrefDeposit(
                article_id=item.article.id,
                environment=settings.CROSSREF_ENV,
                doi=item.doi,
                created_by=created_by,
            )
            db.add(deposit)
            await db.flush()
            return deposit

        # Пересчитанный DOI подхватываем только пока ничего не зарегистрировано
        # (сменили префикс журнала или шаблон суффикса до первой отправки).
        # После 'registered' DOI неизменен: перевыдать его нельзя, старый навеки
        # остаётся в чужих библиографиях.
        if deposit.status in ("pending", "failed") and deposit.doi != item.doi:
            deposit.doi = item.doi
        return deposit
