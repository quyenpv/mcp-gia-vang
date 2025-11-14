# MCP Gia Vang Server

Một máy chủ Model Context Protocol (MCP) cho phép lấy giá vàng từ các nguồn SJC, Doji, PNJ, Phú Quý và Ngọc Thẩm.

Server này sẽ tự động so sánh giá hiện tại với giá được cache (qua Redis hoặc file) và trả về một thông báo đã định dạng.

## Sử dụng

Trong tệp cấu hình MCP Hub của bạn (ví dụ `xiaozhi-mcphub`), thêm vào `mcpServers`:

```json
"mcpServers": {
    "gia-vang": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/quyenpv/mcp-gia-vang", "mcp-gia-vang"]
    },
    "history": {
       ...
    }
}# mcp-gia-vang
