"""
UOF MCP Server — SOAP 客戶端

設計理念：
- UOF 一代平台使用 SOAP/ASMX WebService，與 UOFX 的 REST API 不同
- 本模組封裝所有 SOAP 呼叫的 XML 組裝、HTTP POST 發送、回傳解析
- 不使用 zeep 等重型 SOAP 函式庫，採用 lxml + httpx 輕量封裝
  （避免 WSDL 解析的複雜性，UOF 的 ASMX 端點格式固定且明確）

架構對齊：
- 本模組對應 mcp-uofx/api_client.py 的角色
- 差異在於傳輸協議：SOAP/XML 取代 REST/JSON
"""

import os
import httpx
from typing import Optional, Any
from pathlib import Path
from lxml import etree


def _load_env_file(path: Path) -> None:
    """從指定路徑載入 .env 檔案中的環境變數"""
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip())


# 讀取 repo root 或目前工作目錄的 .env
for env_path in (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"):
    _load_env_file(env_path)


# ── SOAP 命名空間常數 ──────────────────────────────────────────────
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
UOF_NS = "http://tempuri.org/"


class UofSoapClient:
    """
    UOF WebService SOAP 客戶端

    封裝 SOAP Envelope 組裝、HTTP POST 傳送、XML 回傳解析。
    每個 ASMX 端點對應一個固定路徑（如 ~/PublicAPI/System/Authentication.asmx）。
    """

    def __init__(self):
        self.base_url = os.getenv("UOF_BASE_URL", "").rstrip("/")
        self.verify_ssl = os.getenv("UOF_VERIFY_SSL", "true").lower() == "true"

    def _build_soap_envelope(
        self,
        method_name: str,
        params: dict[str, Any],
        namespace: str = UOF_NS,
    ) -> bytes:
        """
        組裝 SOAP Envelope XML

        Args:
            method_name: WebService Method 名稱（如 GetToken、SendForm）
            params: 方法參數字典
            namespace: SOAP 命名空間（預設 http://tempuri.org/）

        Returns:
            UTF-8 encoded SOAP XML bytes
        """
        # 建立 SOAP Envelope
        envelope = etree.Element(
            f"{{{SOAP_NS}}}Envelope",
            nsmap={"soap": SOAP_NS},
        )
        body = etree.SubElement(envelope, f"{{{SOAP_NS}}}Body")

        # 建立方法元素
        method_elem = etree.SubElement(
            body,
            f"{{{namespace}}}{method_name}",
            nsmap={None: namespace},
        )

        # 加入參數
        for param_name, param_value in params.items():
            param_elem = etree.SubElement(method_elem, f"{{{namespace}}}{param_name}")
            if param_value is not None:
                if isinstance(param_value, bool):
                    param_elem.text = str(param_value).lower()
                elif isinstance(param_value, bytes):
                    import base64
                    param_elem.text = base64.b64encode(param_value).decode("ascii")
                else:
                    param_elem.text = str(param_value)

        return etree.tostring(envelope, xml_declaration=True, encoding="utf-8")

    def _parse_soap_response(
        self,
        response_bytes: bytes,
        method_name: str,
        namespace: str = UOF_NS,
    ) -> Optional[str]:
        """
        解析 SOAP Response，提取方法回傳值

        Returns:
            回傳值的文字內容，或 None
        """
        try:
            root = etree.fromstring(response_bytes)
        except etree.XMLSyntaxError as e:
            raise RuntimeError(f"SOAP Response XML 解析失敗: {e}")

        # 尋找 {method_name}Result 元素
        result_tag = f"{{{namespace}}}{method_name}Result"

        # 使用 XPath 搜尋（跨命名空間）
        ns_map = {"ns": namespace, "soap": SOAP_NS}
        results = root.xpath(
            f"//ns:{method_name}Result",
            namespaces=ns_map,
        )

        if results:
            # 如果 Result 有子元素，返回整個 XML 子樹
            if len(results[0]) > 0:
                return etree.tostring(
                    results[0], encoding="unicode", pretty_print=True
                )
            # 否則返回文字內容
            return results[0].text

        # 備用：嘗試找 Body 下的任何結果
        body_results = root.xpath("//soap:Body/*", namespaces=ns_map)
        if body_results and len(body_results) > 0:
            first = body_results[0]
            if len(first) > 0:
                child = first[0]
                if len(child) > 0:
                    return etree.tostring(child, encoding="unicode", pretty_print=True)
                return child.text

        return None

    def call(
        self,
        endpoint_path: str,
        method_name: str,
        params: dict[str, Any],
        namespace: str = UOF_NS,
        timeout: float = 30.0,
    ) -> Optional[str]:
        """
        執行 SOAP 呼叫

        Args:
            endpoint_path: ASMX 端點路徑（如 /PublicAPI/System/Authentication.asmx）
            method_name: WebService Method 名稱
            params: 方法參數字典
            namespace: SOAP 命名空間
            timeout: 請求超時秒數

        Returns:
            方法回傳值的文字內容
        """
        if not self.base_url:
            raise RuntimeError(
                "UOF_BASE_URL is required. Set it in .env or environment variables."
            )
        url = f"{self.base_url}{endpoint_path}"
        soap_action = f"{namespace}{method_name}"
        envelope = self._build_soap_envelope(method_name, params, namespace)

        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{soap_action}"',
        }

        with httpx.Client(verify=self.verify_ssl, timeout=timeout) as client:
            response = client.post(url, content=envelope, headers=headers)
            response.raise_for_status()

        return self._parse_soap_response(response.content, method_name, namespace)

    def call_raw(
        self,
        endpoint_path: str,
        method_name: str,
        params: dict[str, Any],
        namespace: str = UOF_NS,
        timeout: float = 30.0,
    ) -> bytes:
        """
        執行 SOAP 呼叫，回傳原始 XML bytes（供需要自行解析的場景使用）
        """
        if not self.base_url:
            raise RuntimeError(
                "UOF_BASE_URL is required. Set it in .env or environment variables."
            )
        url = f"{self.base_url}{endpoint_path}"
        soap_action = f"{namespace}{method_name}"
        envelope = self._build_soap_envelope(method_name, params, namespace)

        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{soap_action}"',
        }

        with httpx.Client(verify=self.verify_ssl, timeout=timeout) as client:
            response = client.post(url, content=envelope, headers=headers)
            response.raise_for_status()

        return response.content


# 全域單一實例，方便各 Domain 的 service 層調用
uof_client = UofSoapClient()
