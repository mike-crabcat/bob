import time
from urllib.request import Request, urlopen
from urllib.error import URLError
import json

def test_api_call(method, path, data=None):
    url = "http://127.0.0.1:8420/api/v1/projects"
    headers = {"Content-Type": "application/json"}
    body = json.dumps(data).encode() if data else None
    req = Request(url, data=body, headers=headers, method=method)
    
    print(f"Testing {method} {path} with data: {data}")
    start_time = time.time()
    
    try:
        with urlopen(req, timeout=5) as response:
            response_body = response.read()
            if not response_body:
                result = {"data": None}
            else:
                result = json.loads(response_body.decode())
            elapsed = time.time() - start_time
            print(f"Success after {elapsed:.2f}s: {result}")
    except URLError as e:
        elapsed = time.time() - start_time
        print(f"URL Error after {elapsed:.2f}s: {e}")
    except Exception as e:
        elapsed = time.time() - start_time
        print(f"Other error after {elapsed:.2f}s: {e}")

# Test 1: Working case
test_api_call("POST", "/api/v1/projects", {
    "title": "Test API Project",
    "metadata": {"channel": "whatsapp", "chat_id": "test"}
})

# Test 2: Failing case - no metadata
test_api_call("POST", "/api/v1/projects", {
    "title": "Test No Metadata"
})

# Test 3: Failing case - wrong channel
test_api_call("POST", "/api/v1/projects", {
    "title": "Test CLI Channel",
    "metadata": {"channel": "cli", "session_key": "test"}
})
