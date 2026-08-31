# Fubon Neo SDK (Linux 64-bit)

此資料夾放置富邦證券官方提供的 Python Linux 64-bit SDK。

## 使用方式

1. 從富邦新一代 API 官方 SDK 下載頁下載 **Python / Linux 64 位元**版本。
2. 將下載的 `.zip` 或解壓後的 `fubon_neo-*.whl` 上傳到本資料夾。
3. 不要放入帳號、密碼、API key、`.pfx` 憑證或其他秘密資訊。
4. Colab 在 repo 根目錄執行：

```bash
python install_colab_dependencies.py
```

安裝器會：
- 安裝 `requirements-github.txt`
- 若此處只有官方 `.zip`，自動解壓
- 找出 `fubon_neo-*.whl`
- 安裝 wheel
- 驗證 `from fubon_neo.sdk import FubonSDK`

> Fubon Neo Python SDK 並非一般 PyPI 套件，因此不能用 `pip install fubon-neo` 安裝。
