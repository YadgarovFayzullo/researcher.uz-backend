import socket
import sys


def test_socket(host="127.0.0.1", port=5432):
    print(f"Attempting TCP connection to {host}:{port}...")
    try:
        s = socket.create_connection((host, port), timeout=2)
        print("Success! Socket connected.")
        s.close()
        return True
    except Exception as e:
        print(f"Socket connection failed: {e}")
        return False


if __name__ == "__main__":
    success = test_socket()
    if not success:
        sys.exit(1)
