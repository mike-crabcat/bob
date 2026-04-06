#!/usr/bin/env python3
"""Debug script to reproduce the hang issue with project create."""

import sys
import time
from cyborg.cli import _api_call, Settings

def test_api_call_without_server():
    """Test API call when no server is running - this should hang without timeout."""
    print("Testing API call when server is not running...")
    
    start_time = time.time()
    try:
        # This should hang if there's no timeout
        result = _api_call("POST", "/api/v1/projects", {"title": "test"})
        elapsed = time.time() - start_time
        print(f"API call completed in {elapsed:.2f} seconds")
        print(f"Result: {result}")
    except Exception as e:
        elapsed = time.time() - start_time
        print(f"API call failed after {elapsed:.2f} seconds: {e}")

def test_health_endpoint():
    """Test the health endpoint which should be at /health not /api/v1/health."""
    from cyborg.cli import _text_call
    
    print("Testing health endpoint...")
    
    start_time = time.time()
    try:
        result = _text_call("/health")
        elapsed = time.time() - start_time
        print(f"Health call completed in {elapsed:.2f} seconds")
        print(f"Result: {result}")
    except Exception as e:
        elapsed = time.time() - start_time
        print(f"Health call failed after {elapsed:.2f} seconds: {e}")

if __name__ == "__main__":
    print("=== Testing API call hang issue ===")
    test_health_endpoint()
    print("\n=== Testing project create (should hang) ===")
    test_api_call_without_server()
