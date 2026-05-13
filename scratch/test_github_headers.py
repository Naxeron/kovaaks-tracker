import requests
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("test")

def test_check_github_updates():
    urls = [
        "https://raw.githubusercontent.com/Naxeron/kovaaks-tracker/main/scenarios.json",
        "https://raw.githubusercontent.com/Naxeron/kovaaks-tracker/main/scenarios_history.json.gz"
    ]
    
    for url in urls:
        print(f"\nChecking {url}")
        resp = requests.head(url, timeout=10)
        print(f"Status: {resp.status_code}")
        print(f"ETag: {resp.headers.get('ETag')}")
        print(f"Last-Modified: {resp.headers.get('Last-Modified')}")

if __name__ == "__main__":
    test_check_github_updates()
