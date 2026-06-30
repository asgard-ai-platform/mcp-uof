import httpx
from lxml import etree
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
from dotenv import load_dotenv
load_dotenv(ROOT / "mcp-uof/.env")

base_url = os.getenv("UOF_BASE_URL", "").rstrip("/")

services = [
    "/PublicAPI/Album/Album.asmx",
    "/PublicAPI/DMS/Dms.asmx",
    "/PublicAPI/EIP/Bulletin.asmx",
    "/PublicAPI/EIP/ClientChat.asmx",
    "/PublicAPI/EIP/Duty.asmx",
    "/PublicAPI/EIP/PrivateMessage.asmx",
    "/PublicAPI/EIP/UChat.asmx",
    "/PublicAPI/Utility/FileCenter.asmx",
]

def check_service(endpoint):
    url = f"{base_url}{endpoint}?WSDL"
    print(f"\nFetching WSDL from {url}...")
    try:
        resp = httpx.get(url, verify=False, timeout=10.0)
        if resp.status_code == 200:
            # Parse WSDL
            root = etree.fromstring(resp.content)
            ns = {"wsdl": "http://schemas.xmlsoap.org/wsdl/"}
            operations = root.xpath("//wsdl:portType/wsdl:operation", namespaces=ns)
            methods = sorted(list(set(op.get("name") for op in operations)))
            print(f"✅ Success! Methods in {endpoint}:")
            for m in methods:
                print(f"  - {m}")
        else:
            print(f"❌ Failed with status code {resp.status_code}")
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    for s in services:
        check_service(s)
