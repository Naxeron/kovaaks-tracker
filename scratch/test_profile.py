import sys
from api import api_request_with_retry

def main():
    resp = api_request_with_retry("get", "https://kovaaks.com/webapp-backend/profile/users?username=naxeron", timeout=5)
    if resp:
        print(resp.status_code)
        print(resp.text)
    else:
        print("Failed")

if __name__ == "__main__":
    main()
