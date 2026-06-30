# 網頁起單（web apply）設計：共用接口、單種分派、逐單 handler

> 狀態：**已實作、端到端送出成功、且已收斂進 `apply_form`**（見文末「現況」）。
> 本文記錄「為什麼這樣設計」與「採購單實測到的全部細節」，供維護與未來擴充參考。

## 背景：為什麼需要網頁起單

API（SOAP `SendForm`）只能填**中介欄位**。但採購單的本體是一顆客製外掛
`SWUnitechE_POMain`（中介欄位只有 `FORM_NO` + `PO_Main` 兩個，見 [design.md](design.md) 的設計原則）：
主旨、供應商、明細全部包在 plugin 裡，都不是中介欄位，所以 **`apply_form` 填不到它們**
（官方 WebServiceAPI 文件的欄位型態清單裡也根本沒有 plugin/optionalField 這種型態）。

要把這種 plugin 表單**填完整**，唯一的路是走網頁（Playwright）驅動真人填單的 UI。這已實測可行：
供應商、料號的 picker 都能驅動、選取會把資料帶回表單。

這是**寫入**。依既有設計，寫入不做自動 fallback（重放風險，見 design.md），所以網頁起單是一個
**獨立、明確、逐單實作**的能力，不是 `apply_form` 的後備。

## 對外接口：沒有獨立工具，收斂進 `apply_form`

**網頁起單不對外暴露成獨立工具。** 起單一律經 `apply_form`（單一入口）；它拿到 form id 後查設計期
登錄表（registry）決定走網頁 handler 還是 SOAP 中介——對使用者透明。`preview_workflow` 與
`get_form_structure(_by_id)` 同樣依登錄表分派（網頁起單的表單回該單的可填欄位說明 / 試填驗證）。
使用者只挑表單、呼叫同一個工具，**不會遇到「用 web 還是 SOAP 起單」的選擇**（這是刻意的設計）。

`apply_form` 的 `fields` 是**彈性** payload，形狀依表單而定（用 `get_form_structure` 查）：
- 原生表單：`{fieldId: 值}`（明細欄位帶列清單）。
- 網頁起單表單（採購單）：`{subject, supplier, supplier_query?, currency?, payment_term?, location?,
  ship_type_index?, request_date?, details:[{item_code, item_query?, qty, price, unit}]}`（見 handler 的 `describe()`）。

> 接口共用、實作逐單——跟 `BINDING`（工具→機制）同一個精神：對外是穩定的單一面向，「怎麼做」收斂在內部。

## 內部：單種分派（設計期登錄，不 runtime 偵測）

- **怎麼判斷單種**：查 `ops/web_apply/registry.py` 的靜態登錄表——以表單 `form_id`(代碼) 與已知 `version`
  為 key，對應到 web handler 與導航用的表單名稱。**不在 runtime 打 SOAP 讀結構來猜**（呼應 BINDING 精神：
  設計期、靜態、單一決策點）。
- 登錄表（示意）：`採購單 form_id+version → PurchaseOrderWebApplyHandler`。
- **登錄表沒命中** → 該表單走 SOAP 中介起單（`apply_form_structured`）。
- 表單若改版（version 變動），server 會從 `ApplyFormList.aspx` 反查 `formVersionId → formId`，再用登錄表的
  formId 判斷是否走 web handler；因此一般重新發佈不需更新 version。若新部署的 formId GUID 不同，仍需更新登錄表。
- handler 介面（`base.WebApplyHandler`）：`describe()`（可填欄位說明）、`validate(payload)`（開瀏覽器前
  fail-fast）、`fill_and_submit(page, form_name, payload, dry_run)`。
- 與既有 `WebBackend`（query_forms）**共用同一個 Playwright runtime / session**，不另開瀏覽器。

## 逐單 handler：採購單（已完整測繪）

入口流程（實測）：登入 → `ApplyFormList.aspx`（表單申請）→ 展開「請採購」→ 選「採購單」→
「填寫表單」→ 內容在巢狀 iframe `…/DefinedTask/FirstSite.aspx`。

欄位與控制項（實測，名稱以結尾 suffix 表示）：

| 區塊 | 控制項 | 性質 / 操作 |
|---|---|---|
| 主旨 | `txtSubject` | 直接填字 |
| 供應商 | `txtSupplierCode` / `txtSupplierName`（**readonly**）+ `btnVendor` | 開 `SupplierDialog`（940 筆、可搜尋）→ 點代碼列選取，名稱自動帶回 |
| 幣別 | currency `select` | **設了才會啟用「新增細項」**（順序相依）|
| 其他 | 匯率 `txtExchangeRate`、運送方式 `rbShipType`(4 radio)+`txtShipTypeOther`、到貨日 `txtRequestDate` | 直接填/選 |
| 明細 | `btnAdd`（新增細項）→ `POItemDialog` | 見下 |
| 送出 | 流程 `rblFormType`(0/1/2，預設 2) + 「送出」 | |

明細子流程（巢狀 RadWindow）：`btnAdd` → `POItemDialog`（料號 `txtItemCode` **readonly** + `btnQueryItem`）
→ `btnQueryItem` 再開 `ItemDialog`（11663 筆料號、可搜尋）→ 點該列「選取」(`__doPostBack Select$N`) → 按
ItemDialog 的「確定」帶回料號/品名 → 填 `txtItemUnit`/`txtVendorUnit`/`txtQty`/`txtPrice`；「計算」算換算值/
廠商數量單價（若料號無單位換算主檔則以換算=1 直接補上 readonly 欄位）→「確定」加入主表 grid。

巢狀層級：主表(FirstSite) → POItemDialog → ItemDialog（三層 Telerik RadWindow）。

**送出三步（關鍵）**：填完後不能直接送出，順序是
1. **儲存**（`ctl00$MasterPageRadButton1`，畫面「儲存」鈕）：真正把表單存進伺服器。
2. **送出**（`ctl00$MasterPageRadButton3`）：跳出「簽核確認」對話框（下一站點/簽核人員/「確定要結案嗎?」）。
3. 在該對話框按**確定**才真正完成。若申請人就是流程最後一站，會直接 `結案/同意`。

> **底部按鈕用穩定 id，別用按鈕文字。** 儲存/送出是 Telerik RadButton（文字在 `.rbText` span），且
> 「送出前需儲存」之類核取方塊標籤也含「儲存/送出」字樣——`get_by_text("儲存", exact=True)` 容易誤撞或
> 受 postback 時序而 flaky（曾在某些表單逾時找不到）。改用 master 按鈕 id：**儲存**直接觸發
> `__doPostBack('ctl00$MasterPageRadButton1','')`（autoPostBack，等同真實存檔 POST，最穩）；**送出**用
> `#ctl00_MasterPageRadButton3` 真實點擊（需觸發其客戶端流程開確認視窗）。兩者都先 poll 等按鈕出現。

**隱藏 Id companion（關鍵）**：每個下拉都有一個隱藏的 `txt…Id`（`ddPaymentTerm`↔`txtPaymentTermId`、
`ddLocation`↔`txtLocationId`、`ddCurrency`↔`txtCurrencyId`；供應商是 `txtVendorId`）。**伺服器驗證讀的是
隱藏 Id**，只用 `select_option` 設可見下拉、不同步隱藏 Id，必填驗證不會過（紅字不消）。命名規律：
`ddXxx` → `txtXxxId`，值相同。

**供應商 picker 是「依名稱」搜尋**（不是代碼）：`SupplierDialog` 的搜尋框 `txtKey` 搜的是供應商名稱。
所以只給代碼時，只有「在清單第一頁」的代碼點得到；非第一頁的代碼要靠 payload 的 `supplier_query`
（名稱關鍵字）先縮小，再依代碼點選。找不到時 handler 會回明確訊息引導加 `supplier_query`。

**payload fail-fast 驗證**：採購單 handler 的 `validate()` 在開瀏覽器前先擋掉「缺 subject」「缺 supplier」、
「缺 details」、「明細缺 item_code」，回清楚訊息（情境矩陣 N4 即驗此）。選取料號後還會比對
`txtItemCode` 必須等於 payload 的 `item_code`，避免搜尋命中多筆時誤選第一筆。
料號 picker 會先定位包含 `item_code` 的結果列，再點該列「選取」；找不到指定列就停止。

**TaskId 擷取**：送出後先用送出前後 MyFormList 的 PO 單號差集找候選，再打開候選 TaskId 的列印頁比對
本次 `subject`；只有唯一候選符合時才回 TaskId，避免同帳號並行起單時誤抓最大 PO 單號。

## 強韌性規則（測試過程學到的，handler 必須遵守）

這些是把採購單跑過一輪、踩到雷之後歸納的**硬性規則**——之所以要先測，就是為了把它們變成
實作要求，讓這個共用工具對各種單據都站得住：

1. **poll-until-state，不要用固定 sleep。** 每個動作幾乎都觸發 ASP.NET postback，會把 iframe
   整個重建；必須**輪詢目標狀態**（欄位被帶值 / 對話框消失 / 按鈕 enabled）再往下，而不是 sleep 固定秒數。
   固定 sleep 會輸給 postback 的時序競態，導致「點了沒選到」的非決定性失敗（已實測到這種 flaky）。
2. **完成訊號用狀態，不用時間。** 例：供應商選取的完成＝「SupplierDialog 消失」或「`txtSupplierName`
   有值」；料號選取的完成＝「`txtItemCode` 有值」。
3. **開啟 vs 選取，點法不同：**
   - 被遮罩（`TelerikModalOverlay`）擋住的**開啟**按鈕（如新增細項）→ 用 JS `element.click()` 繞遮罩。
   - **會回呼父視窗的選取／確定**→ 要用**真實 click**；JS `.click()` 不一定觸發 RadWindow 的回呼。
   - **不可見的 GridView 選取連結**（料號列的「選取」在被裁切的欄位裡）→ 直接執行該列的
     `__doPostBack('…$gvMain','Select$N')`（讀連結的 href 來跑）。
4. **順序相依要編碼進流程：** 幣別先設 → 才能新增明細。
5. **失敗就停、不送半張。** 任一必填/選取沒成功就中止，不要送出殘缺單。
6. **底部按鈕用穩定 id、跨 frame 找，別用按鈕文字。** 儲存/送出是 Telerik master 按鈕
   （`ctl00$MasterPageRadButton1`/`3`，文字在 `.rbText` span）；用 `get_by_text("儲存")` 會誤撞
   「送出前需儲存」標籤、且受 postback 時序而 flaky（曾實機逾時找不到）。改用 master id、**掃所有
   frame**（不假設在 FirstSite frame）：儲存走 `__doPostBack`（autoPostBack，等同存檔 POST，且
   **不需按鈕可見**——解「在捲動區外未渲染」）；送出需真實點擊（觸發開確認視窗）先捲到可見再點。
   找不到時 dump 各 frame 按鈕線索進錯誤訊息（可診斷而非死路）。
7. **對話框 handler 只裝一次（長壽 page 的陷阱）。** runtime 的 page 是單例（一個 server 程序共用）；
   若每次起單都 `page.on("dialog", …)`，handler 會**累加**，之後一個對話框被多個 handler 各 `accept()`
   一次 → Playwright「Cannot accept dialog which is already handled!」整個起單崩潰。**長時間運行的
   Desktop server 多次起單後必中**（本機單次測試會漏掉——務必以「同一程序連續多次起單」重現）。做法：
   以 page 屬性 guard，handler 只註冊一次，`accept()` 包 try 防重複。
8. **清理紀律：** 測試起出的單一律 `terminate_task(task_id, "Cancel", reason)` 作廢（沿用既有測試規範）。

## 測試情境矩陣（待逐一驗證，用來硬化 handler）

| 維度 | 情境 |
|---|---|
| 供應商 | 直接選代碼 / 用關鍵字搜尋過濾後選 |
| 幣別 | TWD / 外幣（牽動匯率、換算值）|
| 運送方式 | 四個 radio 各一 + 「其他」要填文字 |
| 明細 | 1 筆 / 多筆 / 不同料號 / 數量單價邊界值 |
| 必填驗證 | 缺主旨 / 缺供應商 / 無明細 → 送出應被擋（記錄錯誤訊息與行為）|
| 流程 | 固定流程 vs 自由流程（`rblFormType`）+ 第一關簽核者設定 |

## 現況與下一步

- **已實作、端到端送出成功、且已收斂進 `apply_form`。** `ops/web_apply/`（`registry` + `router`
  + `PurchaseOrderWebApplyHandler` + `helpers`）完成；起單經 `apply_form` 依 registry 分派。實測以 web
  起出採購單 **PO260600106(通過)**：主旨／供應商／幣別／付款條件／儲存地點／運送方式／到貨日＋明細全填，
  儲存→送出→簽核確認完成。情境矩陣 8→修正後 10/10（多明細、外幣、二帳號、負向）。
- **決定性的兩個關鍵**（由使用者提供的儲存 POST 解出）：**隱藏 Id companion** 與 **儲存→送出→簽核確認**
  三步（見上）。加上事件式同步（poll-until-state、等對話框開/關、選取後按確定）後即穩定，不再 flaky。
- **下一步：** 「routing 到下一站簽核者」（目前申請人即最後一站，送出會直接結案）以利重複測試可作廢；
  registry 改吃 config/env（目前 GUID 寫死於程式，新部署/改版需更新）；多 handler 時把結果格式
  （`_format` 內 PO 專屬字樣）移到 handler。

## 在程式架構中的位置

- **不**進 `apply_form` 的自動 fallback——起單依 registry **設計期分派**（plugin 表單→web、其餘→SOAP），
  不是「SOAP 失敗才退 web」。
- web-apply 子系統：`registry`（設計期登錄 form→handler）+ `router`（`resolve`/`describe`/`apply_web`）+
  逐單 `handler`。**不對外暴露獨立工具**——收斂進 `apply_form`/`preview_workflow`/`get_form_structure`。
- 重用既有 `WebBackend` 的 Playwright runtime 與 session（同一身份、同一瀏覽器）。
- 兩軸分派的關係見 [architecture.md](architecture.md)「起單的兩軸分派」。
