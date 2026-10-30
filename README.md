# 履歷篩選 MVP

求職者在網頁填寫履歷，後台人員用表格檢視、點入看完整履歷、標記篩選狀態，並可用本機 LM Studio 讓 AI 依職缺條件自動評分。
只用 Python 標準函式庫加 SQLite，**不需要安裝任何套件**，之後可直接用 PyInstaller 打包成 exe。

## 快速開始

```bash
python server.py            # 需要 Python 3.10 以上
```

- 投遞頁：<http://127.0.0.1:8000/>
- 後台：<http://127.0.0.1:8000/admin>（預設密碼 `admin`，登入後請到「設定」修改）

常用參數：`--port 9000`、`--host 0.0.0.0`（讓區網其他電腦連得到）、`--no-browser`（不自動開瀏覽器）。
資料存在同資料夾的 `resumes.db`，備份這個檔案就等於備份所有資料。

## 功能

| 頁面 | 功能 |
| --- | --- |
| 投遞頁 `/` | 依後台設定動態產生的履歷表，有必填、Email、電話、數字等格式檢查，手機可用 |
| 後台・履歷列表 | 表格呈現，可搜尋任何欄位、依狀態篩選、匯出 CSV（Excel 可直接開，中文不亂碼） |
| 後台・履歷詳細頁 | 點列表任一列進入，顯示完整履歷，可改狀態（新投遞／審核中／面試／錄取／不合適）、寫內部備註、刪除 |
| 後台・履歷欄位設定 | 新增、刪除、排序欄位；可改名稱、類型、提示文字、說明、選項；勾選「必填」與「顯示在後台列表」 |
| 後台・AI 篩選 | 啟用 AI 評分、填寫職缺條件、設定 LM Studio 連線並測試 |
| 後台・設定 | 修改後台密碼 |

支援的欄位類型：單行文字、多行文字（自傳）、Email、電話、數字、日期、下拉選單、單選、多選。
預設欄位：姓名、電話號碼、Email、地址、學校、最高學歷、應徵職位、工作年資、專長技能、自傳。

刪除欄位不會刪掉舊資料，詳細頁會以「已移除的欄位」顯示。

## AI 篩選（LM Studio）

作法依照課程講義（My first AI Agent with LM Studio、Agent Loop、Pydantic 驗證、Multi-Tool Agent）：

1. 在 LM Studio 下載並載入 `qwen/qwen3-4b-2507`，到 Developer 分頁啟動 Local Server（預設 `http://127.0.0.1:1234/v1`）。
2. 後台「AI 篩選」頁勾選「啟用 AI 評分」，填寫職缺條件，按「測試連線」確認，再儲存。
3. 之後每份新履歷會自動排隊評分；舊履歷可在列表按「AI 評分未評的履歷」，或在詳細頁按「重新評分」。
4. 列表會多出「AI 評分」欄，可依 AI 建議篩選、依分數排序；詳細頁顯示分數、建議與評語。CSV 也會帶上 AI 欄位。

評分流程（`llm.py`）：

- **決策呼叫**：system prompt 規定只能輸出 `SCORE:0～100` 和 `VERDICT:推薦／待定／不推薦` 兩行，`temperature=0`。
- **驗證**：用字串解析，再用白名單檢查分數範圍與建議選項（相當於講義中 Pydantic 的 `Literal` 與 validator，但不需要額外安裝套件）。全形冒號、`<think>` 區塊都會先處理掉。
- **錯誤回饋重試**：格式不對時，把模型的輸出和 `Observation: 上一個輸出未通過格式驗證…` 加回對話請它重寫，最多 `MAX_STEPS = 3` 次。
- **最後答案呼叫**：另外請模型寫評語，規定「只能根據履歷內容，不可以編造」、繁體中文、150 字內。
- 呼叫 LM Studio 用標準函式庫 `urllib` 直接打 `POST /v1/chat/completions`（與講義中 `openai` SDK 的請求相同），所以 exe 不用多帶套件。
- 評分在單一背景執行緒排隊處理，避免同時塞爆本機模型；伺服器重開時，沒評完的會自動重新排入。連不到 LM Studio 時，該筆會顯示「評分失敗」和原因。

> 講義的提醒同樣適用：格式驗證只保證「輸出結構正確」，不保證判斷正確。AI 分數請當作排序與初篩參考，最後仍由人工決定。

## 打包成 exe

在 Windows 上雙擊 `build_exe.bat`（或 macOS / Linux 執行 `./build_exe.sh`），會產生 `dist/ResumeScreener.exe`。
雙擊 exe 就會啟動伺服器並自動開啟後台，資料庫 `resumes.db` 建在 exe 旁邊。
（已在 Linux 上實測 PyInstaller 打包與執行成功；Windows 版需在 Windows 電腦上打包。）

## 部署到網路上

這是單一 Python 程式，任何能跑 Python 的主機都可以：

```bash
python server.py --host 0.0.0.0 --port 8000 --no-browser
```

正式對外時建議前面加一層 Nginx / Caddy 提供 HTTPS，並記得改掉預設密碼。

## 檔案結構

```
server.py          後端（HTTP、API、SQLite、驗證、AI 評分排程）
llm.py             LM Studio 呼叫、輸出驗證與重試
static/apply.*     投遞頁
static/admin.*     後台（單頁應用，以 #/ 路由切換頁面）
static/style.css   共用樣式
build_exe.bat/.sh  打包腳本
```

## API 一覽

| 方法 | 路徑 | 說明 |
| --- | --- | --- |
| GET | `/api/form` | 取得表單標題與欄位設定 |
| POST | `/api/resumes` | 投遞履歷 `{ data: { 欄位代碼: 值 } }` |
| POST | `/api/admin/login` / `logout` | 登入、登出 |
| GET | `/api/admin/resumes?q=&status=` | 列表（只回傳「顯示在列表」的欄位） |
| GET / PUT / DELETE | `/api/admin/resumes/{id}` | 單筆詳細、更新狀態備註、刪除 |
| GET | `/api/admin/resumes.csv` | 匯出 CSV |
| PUT | `/api/admin/form` | 儲存欄位設定 |
| PUT | `/api/admin/password` | 修改密碼 |
| GET / PUT | `/api/admin/llm` | 讀取、儲存 AI 設定 |
| POST | `/api/admin/llm/test` | 測試 LLM 連線 |
| POST | `/api/admin/resumes/{id}/screen` | 對單筆重新評分 |
| POST | `/api/admin/screen-all` | 把未評分或失敗的履歷全部排入 |

## 下一步建議

1. 用真的 LM Studio 和幾份真實履歷校準職缺條件寫法與分數門檻。
2. 附件上傳（PDF 履歷、大頭照）。
3. 多個職缺各自一套表單與職缺條件。
4. 多位後台帳號與權限。
