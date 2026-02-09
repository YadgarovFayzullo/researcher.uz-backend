import psycopg2
import sys

# URL forced to 127.0.0.1
DSN = "postgresql://postgres:postgres@127.0.0.1:5432/scientific_db"


def test_connection():
    print(f"Connecting to {DSN} using psycopg2...")
    try:
        conn = psycopg2.connect(DSN)
        cur = conn.cursor()
        cur.execute("SELECT version()")
        version = cur.fetchone()[0]
        print(f"Connected! Server version: {version}")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Failed: {e}")
        # If psycopg2 is not installed, this script will fail at import, which is expected info.


if __name__ == "__main__":
    test_connection()
