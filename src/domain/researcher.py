"""Кабинет исследователя — порт `supabase/researcher_cabinet.sql` + get_researcher_profile.

Все RPC в SQL — SECURITY DEFINER, переопределяют личность через auth.uid()
и привязанный orcid_id, поэтому пользователь может действовать только над своим
профилем / своими работами. Здесь личность приходит из JWT (get_current_profile),
а сервис принимает user_id и сам подтягивает orcid из profiles.
"""
from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

import re

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.demo import (
    article_is_demo,
    is_demo_profile,
    meta_is_demo,
    profile_is_demo,
    profile_is_not_demo,
)
from src.domain.author_names import (
    DIGRAPHS,
    STANDALONE_SUFFIX,
    clean,
    may_be_same_person,
    translit,
)
from src.infrastructure.persistence.models import (
    Article,
    ArticleAuthor,
    Author,
    AuthorClaim,
    Profile,
    ResearcherWork,
)

_DOI_PREFIX = re.compile(r"^https?://(dx\.)?doi\.org/", re.IGNORECASE)


def _profile_name_parts(full_name: str | None) -> tuple[list[str], list[str]]:
    """ФИО профиля → (полные слова, инициалы) в алфавите ключей карточек.

    Разбор повторяет `identity_key`: транслитерация до проверки на инициал
    («Ш.» → «sh» — это инициал, а не слово), отдельные «qizi»/«o'g'li» выкинуты.
    """
    tokens = [
        translit(t) for t in re.split(r"[\s.]+", clean(full_name or "").lower()) if t
    ]
    tokens = [t for t in tokens if t and t not in STANDALONE_SUFFIX]
    is_initial = lambda t: len(t) == 1 or (len(t) == 2 and t in DIGRAPHS)  # noqa: E731
    words = [t for t in tokens if not is_initial(t)]
    initials = [t[0] for t in tokens if is_initial(t)]
    return words, initials


def _within(a: list[str], b: list[str]) -> bool:
    """Мультимножество a целиком входит в b."""
    return not (Counter(a) - Counter(b))


def _fold(word: str) -> str:
    """Имя для сравнения написаний: «Farhod» = «Фарход» (farxod), «Yorqinoy» = «Yorkinoy»."""
    w = re.sub(r"['\-]", "", word.lower())
    return w.replace("kh", "x").replace("h", "x").replace("q", "k")


def _same_given_name(a: str, b: str) -> bool:
    fa, fb = _fold(a), _fold(b)
    # Префикс — для «Muhammad» против «Muhammadyusuf»; короче 4 букв не
    # доверяем, иначе «Ali» совпал бы с «Alisher» и «Alirizo» разом.
    return fa == fb or (
        min(len(fa), len(fb)) >= 4 and (fa.startswith(fb) or fb.startswith(fa))
    )


def card_matches_name(
    name_key: str, full_name: str | None, display_name: str | None = None
) -> bool:
    """Похожа ли карточка автора (по её `name_key`) на ФИО из профиля.

    Сравнение мягче, чем склейка карточек: профиль пишут «Fayzullo Yadgarov»,
    а под статьёй стоит «Yadgarov F.B.», и точный ключ тут не совпадёт никогда.
    Мягкость допустима, потому что результат — только подсказка: привязку
    по-прежнему решает владелец платформы по заявке «Это я».

    `display_name` (самое полное написание карточки) отсекает однофамильцев,
    которых ключ не различает: у «Boymatov Bahrom» ключ тот же `boymatov|b…`,
    что подошёл бы «Bekzod Boymatov», но имя на ту же букву — другое.
    """
    words, initials = _profile_name_parts(full_name)
    # Одно слово — как и в identity_key: за ним разные люди.
    if len(words) < 2:
        return False

    head, _, tail = name_key.partition("|")
    card_words = [w for w in head.split("+") if w]
    card_initials = [i for i in tail.split(".") if i]

    if len(card_words) >= 2:
        # Карточка неизвестного порядка «имя+фамилия»: все её слова есть в ФИО
        # (или наоборот — в ФИО нет отчества, а в карточке оно словом).
        return len(set(card_words) & set(words)) >= 2 and (
            set(card_words) <= set(words) or set(words) <= set(card_words)
        )

    if len(card_words) != 1 or card_words[0] not in words or not card_initials:
        return False
    surname = card_words[0]
    mine = [w[0] for w in words if w != surname] + initials
    # Инициалы не должны спорить: одна сторона полнее другой (в профиле нет
    # отчества, в подписи есть), но общая буква обязательна.
    if not (set(mine) & set(card_initials)) or not (
        _within(card_initials, mine) or _within(mine, card_initials)
    ):
        return False

    if display_name:
        # Полные имена карточки на ту же букву, что имя в профиле, должны с ним
        # совпасть. Отчество («…ovich») именем не считаем: «Bekzod» против
        # «Bekzod Bahodirovich» — это не спор.
        theirs = [
            w
            for w in _profile_name_parts(display_name)[0]
            if _fold(w) != _fold(surname)
            and not re.search(r"(ovich|evich|ovna|evna)$", w)
        ]
        for given in (w for w in words if w != surname):
            rivals = [w for w in theirs if w[0] == given[0]]
            if rivals and not any(_same_given_name(given, r) for r in rivals):
                return False
    return True


def _norm_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    return _DOI_PREFIX.sub("", doi.strip()).lower() or None


class CabinetError(Exception):
    """Ошибка бизнес-правила кабинета (нет ORCID, статья не найдена и т.п.)."""


def _nullif_empty(value: str | None) -> str | None:
    """SQL nullif(x, '') — пустую строку превращаем в None."""
    if value is None:
        return None
    return value or None


class ResearcherDomain:
    async def public_profiles(
        self, db: AsyncSession, limit: int = 12
    ) -> list[dict[str, Any]]:
        """Публичные профили с аватаром — для слайдера на главной.

        Отдаём только то, что и так видно на публичной карточке исследователя:
        имя, аватар, место работы, должность, страна. Ни email, ни роли здесь
        быть не должно — это анонимный эндпоинт.
        """
        rows = (
            await db.execute(
                select(
                    Profile.id,
                    Profile.full_name,
                    Profile.avatar_url,
                    Profile.workplace,
                    Profile.position,
                    Profile.country,
                    Profile.orcid_id,
                    Profile.created_at,
                )
                .where(
                    Profile.is_public.is_(True),
                    Profile.avatar_url.isnot(None),
                    Profile.avatar_url != "",
                    # Демо-профиль для показа дизайнеру/клиенту — не витрина.
                    profile_is_not_demo(),
                )
                .order_by(Profile.created_at.desc().nullslast())
                .limit(max(1, min(limit, 50)))
            )
        ).all()
        return [
            {
                "id": str(r.id),
                "full_name": r.full_name,
                "avatar_url": r.avatar_url,
                "workplace": r.workplace,
                "position": r.position,
                "country": r.country,
                "orcid_id": r.orcid_id,
                "created_at": r.created_at,
            }
            for r in rows
        ]

    async def _profile(self, db: AsyncSession, user_id) -> Profile:
        prof = (
            await db.execute(select(Profile).where(Profile.id == user_id))
        ).scalars().first()
        if prof is None:
            raise CabinetError("profile not found")
        return prof

    async def suggest_author_cards(
        self, db: AsyncSession, *, user_id, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Непривязанные карточки авторов, похожие на ФИО профиля.

        Карточку с отклонённой заявкой не подсказываем: владелец уже решил, и
        баннер не должен звать подавать её снова (со страницы карточки повторная
        заявка по-прежнему возможна).
        """
        prof = await self._profile(db, user_id)
        # Демо-профилю подсказывать чужие карточки незачем: заявка на них у него
        # всё равно закрыта (authors.request_claim).
        if is_demo_profile(prof):
            return []
        words, _ = _profile_name_parts(prof.full_name)
        if len(words) < 2:
            return []

        # Грубый отбор в SQL, точный — card_matches_name. Ключи бывают двух форм:
        # «фамилия|инициалы» — ищем по началу, «слово+слово» — нужны два слова
        # сразу, иначе частое имя («dilnoza») притащило бы сотни чужих карточек.
        like = lambda w: Author.name_key.contains(w, autoescape=True)  # noqa: E731
        conds = [Author.name_key.startswith(f"{w}|", autoescape=True) for w in words]
        conds += [
            like(a) & like(b) for i, a in enumerate(words) for b in words[i + 1:]
        ]
        rows = (
            await db.execute(
                select(
                    Author.id,
                    Author.slug,
                    Author.display_name,
                    Author.works_count,
                    Author.name_key,
                )
                .where(
                    Author.profile_id.is_(None),
                    Author.works_count > 0,
                    or_(*conds),
                )
                .order_by(Author.works_count.desc(), Author.slug)
                .limit(200)
            )
        ).all()
        cards = [
            r
            for r in rows
            if card_matches_name(r.name_key, prof.full_name, r.display_name)
        ]
        if not cards:
            return []

        claims = (
            await db.execute(
                select(AuthorClaim.author_id, AuthorClaim.status)
                .where(
                    AuthorClaim.profile_id == prof.id,
                    AuthorClaim.author_id.in_([c.id for c in cards]),
                )
                .order_by(AuthorClaim.created_at)
            )
        ).all()
        # По порядку created_at: последняя заявка перетирает прежние.
        latest = {c.author_id: c.status for c in claims}

        out: list[dict[str, Any]] = []
        for c in cards:
            status = latest.get(c.id, "none")
            if status == "rejected":
                continue
            out.append(
                {
                    "slug": c.slug,
                    "display_name": c.display_name,
                    "works_count": c.works_count,
                    "claim_status": status,
                }
            )
        return out[:limit]

    # ------------------------------------------------------------------ #
    async def update_my_profile(
        self,
        db: AsyncSession,
        *,
        user_id,
        full_name: str | None = None,
        workplace: str | None = None,
        country: str | None = None,
        education: str | None = None,
        bio: str | None = None,
        avatar_url: str | None = None,
    ) -> None:
        """Порт update_my_profile: NULL = не трогать, '' = очистить поле."""
        prof = await self._profile(db, user_id)
        # case when p_x is null then x else nullif(p_x, '') end
        if full_name is not None:
            prof.full_name = _nullif_empty(full_name)
        if workplace is not None:
            prof.workplace = _nullif_empty(workplace)
        if country is not None:
            prof.country = _nullif_empty(country)
        if education is not None:
            prof.education = _nullif_empty(education)
        if bio is not None:
            prof.bio = _nullif_empty(bio)
        if avatar_url is not None:
            prof.avatar_url = _nullif_empty(avatar_url)
        prof.updated_at = func.now()
        await db.commit()

    # ------------------------------------------------------------------ #
    async def _author_identity(
        self, db: AsyncSession, user_id
    ) -> tuple[str | None, str]:
        """ORCID (если привязан) и имя для строки автора.

        ORCID здесь перестал быть обязательным: профиль есть у любого
        зарегистрированного, в том числе вошедшего через Google, и прицепить
        свою статью он должен мочь без iD — связь тогда держит только
        `article_authors.profile_id`, по нему же собирается его страница.
        """
        prof = await self._profile(db, user_id)
        name = (prof.full_name or "").strip() or "Автор"
        return (prof.orcid_id or None), name

    @staticmethod
    def _claimed_by(user_id, orcid: str | None):
        """Условие «строка автора уже принадлежит этому профилю».

        Сравнение по ORCID подмешиваем, только когда он есть: `orcid == None`
        SQLAlchemy разворачивает в `orcid IS NULL`, и условие совпало бы с
        любым автором без iD — чужая статья считалась бы уже привязанной.
        """
        cond = ArticleAuthor.profile_id == user_id
        if orcid:
            cond = cond | (ArticleAuthor.orcid == orcid)
        return cond

    async def _next_author_order(self, db: AsyncSession, article_id: int) -> int:
        # coalesce(max(author_order) + 1, 0)
        mx = (
            await db.execute(
                select(func.max(ArticleAuthor.author_order)).where(
                    ArticleAuthor.article_id == article_id
                )
            )
        ).scalar()
        return (mx + 1) if mx is not None else 0

    async def claim_article(self, db: AsyncSession, *, user_id, article_id: int) -> None:
        """Порт claim_article: привязать статью к профилю (idempotent)."""
        v_orcid, v_name = await self._author_identity(db, user_id)

        exists_article = (
            await db.execute(select(Article.id).where(Article.id == article_id))
        ).scalars().first()
        if exists_article is None:
            raise CabinetError("article not found")
        # Демо-профиль подписывается только под демо-статьями: иначе кнопка
        # «Прикрепить», ради которой его и показывают, сделала бы вымышленного
        # человека подписантом настоящей статьи.
        if await profile_is_demo(db, user_id) and not await article_is_demo(db, article_id):
            raise CabinetError("demo profile can attach only demo articles")

        # Уже на этом профиле? (по profile_id или orcid)
        already = (
            await db.execute(
                select(ArticleAuthor.id).where(
                    ArticleAuthor.article_id == article_id,
                    self._claimed_by(user_id, v_orcid),
                )
            )
        ).scalars().first()
        if already is not None:
            return

        signature = await self._own_signature(db, article_id, v_name)
        if signature is not None:
            # Человек уже подписан под статьёй — привязываем СУЩЕСТВУЮЩУЮ
            # подпись, а не дописываем вторую. Иначе у статьи появляется лишний
            # автор (а `articles.authors` говорит обратное), человек попадает
            # к себе же в соавторы, и из его второго написания вырастает
            # вторая карточка автора.
            signature.profile_id = user_id
            signature.orcid = signature.orcid or v_orcid
            signature.is_verified = True
        else:
            # Подписи нет: в метаданных статьи человека забыли. Заводим свою
            # строку и помечаем её — это не подпись из статьи.
            order = await self._next_author_order(db, article_id)
            db.add(
                ArticleAuthor(
                    article_id=article_id,
                    author_order=order,
                    author_name=v_name,
                    orcid=v_orcid,
                    profile_id=user_id,
                    is_verified=True,
                    from_claim=True,
                )
            )
        await db.commit()

    async def _own_signature(
        self, db: AsyncSession, article_id: int, name: str
    ) -> ArticleAuthor | None:
        """Ничья подпись под статьёй, похожая на имя заявителя.

        Отдаёт строку, только если подходящая РОВНО ОДНА: у статьи двух
        Ядгаровых подпись не угадать, и тогда честнее завести отдельную —
        владелец разберёт это заявкой на карточку.
        """
        rows = list(
            (
                await db.execute(
                    select(ArticleAuthor).where(
                        ArticleAuthor.article_id == article_id,
                        ArticleAuthor.profile_id.is_(None),
                        ArticleAuthor.orcid.is_(None),
                        ArticleAuthor.from_claim.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )
        found = [r for r in rows if may_be_same_person(name, r.author_name or "")]
        return found[0] if len(found) == 1 else None

    async def unclaim_article(
        self, db: AsyncSession, *, user_id, article_id: int
    ) -> None:
        """Отцепить статью от профиля.

        Строку, заведённую кабинетом, удаляем; настоящую подпись из статьи
        только отвязываем — удалить её значило бы стереть автора из метаданных
        чужой статьи одним нажатием.
        """
        # auth.uid() null check обеспечивает get_current_profile.
        v_orcid, _ = await self._author_identity(db, user_id)
        await db.execute(
            ArticleAuthor.__table__.delete().where(
                ArticleAuthor.article_id == article_id,
                ArticleAuthor.profile_id == user_id,
                ArticleAuthor.from_claim.is_(True),
            )
        )
        # Свой iD с подписи снимаем тоже: профиль по ORCID собирается по нему,
        # и статья иначе осталась бы в списке. Чужой iD в строке не трогаем.
        values: dict[str, Any] = {"profile_id": None, "is_verified": False}
        await db.execute(
            ArticleAuthor.__table__.update()
            .where(
                ArticleAuthor.article_id == article_id,
                ArticleAuthor.profile_id == user_id,
            )
            .values(**values)
        )
        if v_orcid:
            await db.execute(
                ArticleAuthor.__table__.update()
                .where(
                    ArticleAuthor.article_id == article_id,
                    ArticleAuthor.orcid == v_orcid,
                )
                .values(orcid=None)
            )
        await db.commit()

    async def claim_articles_by_dois(
        self, db: AsyncSession, *, user_id, dois: list[str]
    ) -> int:
        """Порт claim_articles_by_dois: bulk-привязка по DOI. Возвращает число новых."""
        if await profile_is_demo(db, user_id):
            raise CabinetError("demo profile cannot import works by DOI")
        v_orcid, v_name = await self._author_identity(db, user_id)
        wanted = {d for d in (_norm_doi(x) for x in (dois or [])) if d}
        if not wanted:
            return 0

        # Кандидаты: статьи с DOI, чей нормализованный DOI в наборе, ещё не
        # привязанные к этому профилю/ORCID.
        rows = (
            await db.execute(
                select(Article.id, Article.doi).where(
                    Article.doi.isnot(None), Article.doi != ""
                )
            )
        ).all()

        count = 0
        for aid, doi in rows:
            if _norm_doi(doi) not in wanted:
                continue
            already = (
                await db.execute(
                    select(ArticleAuthor.id).where(
                        ArticleAuthor.article_id == aid,
                        self._claimed_by(user_id, v_orcid),
                    )
                )
            ).scalars().first()
            if already is not None:
                continue
            # Как в claim_article: своя подпись под статьёй привязывается, а не
            # дублируется второй строкой.
            signature = await self._own_signature(db, aid, v_name)
            if signature is not None:
                signature.profile_id = user_id
                signature.orcid = signature.orcid or v_orcid
                signature.is_verified = True
            else:
                order = await self._next_author_order(db, aid)
                db.add(
                    ArticleAuthor(
                        article_id=aid,
                        author_order=order,
                        author_name=v_name,
                        orcid=v_orcid,
                        profile_id=user_id,
                        is_verified=True,
                        from_claim=True,
                    )
                )
            count += 1

        await db.commit()
        return count

    async def import_orcid_works(
        self, db: AsyncSession, *, user_id, works: list[dict]
    ) -> int:
        """Порт import_orcid_works: полный replace внешних работ (ORCID = источник истины).

        Возвращает число сохранённых работ.
        """
        prof = await self._profile(db, user_id)
        if is_demo_profile(prof):
            raise CabinetError("demo profile cannot import ORCID works")
        if not prof.orcid_id:
            raise CabinetError("no ORCID linked")
        v_orcid = prof.orcid_id

        # delete all, потом вставляем актуальные (works removed in ORCID disappear).
        await db.execute(
            ResearcherWork.__table__.delete().where(ResearcherWork.orcid == v_orcid)
        )

        seen: set[str] = set()
        for w in works or []:
            put_code = (w.get("put_code") or "").strip()
            if not put_code or put_code in seen:  # on conflict (orcid, put_code) do nothing
                continue
            seen.add(put_code)
            year = w.get("year")
            try:
                year_val = int(year) if year not in (None, "") else None
            except (TypeError, ValueError):
                year_val = None
            db.add(
                ResearcherWork(
                    orcid=v_orcid,
                    put_code=put_code,
                    title=_nullif_empty(w.get("title")),
                    work_type=_nullif_empty(w.get("work_type")),
                    year=year_val,
                    doi=_nullif_empty(w.get("doi")),
                    url=_nullif_empty(w.get("url")),
                    container=_nullif_empty(w.get("container")),
                )
            )

        await db.commit()
        n = (
            await db.execute(
                select(func.count())
                .select_from(ResearcherWork)
                .where(ResearcherWork.orcid == v_orcid)
            )
        ).scalar()
        return int(n or 0)

    async def get_researcher_profile(
        self, db: AsyncSession, orcid: str
    ) -> dict | None:
        """Порт get_researcher_profile: публичная карточка по ORCID (без id/role/email)."""
        row = (
            await db.execute(
                select(
                    Profile.full_name,
                    Profile.orcid_id,
                    Profile.avatar_url,
                    Profile.workplace,
                    Profile.country,
                    Profile.bio,
                    Profile.education,
                    Profile.meta,
                ).where(Profile.orcid_id == orcid)
            )
        ).first()
        if row is None:
            return None
        return {
            "full_name": row.full_name,
            "orcid": row.orcid_id,
            "avatar_url": row.avatar_url,
            "workplace": row.workplace,
            "country": row.country,
            "bio": row.bio,
            "education": row.education,
            # Фронт ставит демо-профилю noindex.
            "is_demo": meta_is_demo(row.meta),
        }

    async def get_profile_by_user_id(
        self, db: AsyncSession, user_id: str) -> dict | None:
        """Та же публичная карточка, но по id аккаунта — для тех, у кого ORCID нет.

        Профиль заводится при любой регистрации (см. `AuthDomain.
        get_or_create_oauth_user`), а вот адресовать его было нечем: публичная
        страница ходила только по ORCID, и вошедший через Google упирался в
        «профиля нет». Поля и их набор — как в `get_researcher_profile`, чтобы
        страница отрисовала обе карточки одним компонентом.

        `is_public = false` прячет карточку (404): флаг для того и заведён, а на
        ORCID-странице он не проверяется только потому, что там адрес и так
        знает лишь владелец iD.
        """
        try:
            uid = uuid.UUID(str(user_id))
        except (TypeError, ValueError):
            return None

        row = (
            await db.execute(
                select(
                    Profile.full_name,
                    Profile.orcid_id,
                    Profile.avatar_url,
                    Profile.workplace,
                    Profile.country,
                    Profile.bio,
                    Profile.education,
                    Profile.meta,
                ).where(Profile.id == uid, Profile.is_public.isnot(False))
            )
        ).first()
        if row is None:
            return None
        return {
            "full_name": row.full_name,
            "orcid": row.orcid_id,
            "avatar_url": row.avatar_url,
            "workplace": row.workplace,
            "country": row.country,
            "bio": row.bio,
            "education": row.education,
            # Фронт ставит демо-профилю noindex.
            "is_demo": meta_is_demo(row.meta),
        }

    async def list_researcher_works(
        self, db: AsyncSession, orcid: str
    ) -> list[ResearcherWork]:
        """Внешние работы исследователя (researcher_works, public read)."""
        rows = (
            await db.execute(
                select(ResearcherWork)
                .where(ResearcherWork.orcid == orcid)
                .order_by(ResearcherWork.year.desc().nullslast())
            )
        ).scalars().all()
        return list(rows)
