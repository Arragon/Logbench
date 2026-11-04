# M4 Gate · 私有上传到索引查询的闭环（R6-051）

- 结论：**PASS**
- 时间：2026-11-03 12:32:12（耗时 116.6 s）
- 用例：5 通过 / 0 失败
- 输入：6 MiB 生成日志（确定性模板）

## 环境

| 项 | 值 |
| --- | --- |
| python | 3.9.11 |
| platform | macOS-27.0.1-arm64-arm-64bit |
| base | http://127.0.0.1:63535 |
| 传输 | server.httpd.Server（真实 socket） |
| 客户端 | urllib + Playwright(system Chrome) |
| 请求数 | 217，响应字节 388597 |

## 命令

```
env -u PYTHONPATH ~/.local/bin/python3.9 qa/r6/private_acceleration_gate.py
env -u PYTHONPATH ~/.local/bin/python3.9 qa/r6/private_acceleration_gate.py --report-only
```

## 关键数字

| 项 | 值 |
| --- | --- |
| 输入 | 6291739 B / 48470 行，sha256 `9ef8628c22727a60…` |
| 分片上传 | 2 片 / 6291739 B，0.05 s（120.0 MB/s） |
| 封存 | state=sealed，6291739 B，content_sig `9ef8628c22727a60…` |
| 服务端索引 | 37.20 s（indexing → indexing → indexing → indexed） |
| 宽查询命中 | 48470 条（relation=exact，plan=postings，1.93 s） |
| 扫描覆盖 | 6291739 / 6291739 B，complete=True，mode=postings |
| 窄查询命中 | 9 条（ordinal [5000, 10001, 15002, 20003, 25004]） |
| 导出 | HTTP 200，5 行，X-LogBench-Export-Rows=5，truncated=true |
| 跨身份访问 | 5/5 条路由返回 404（对端 10.9.9.9） |
| 断点续传 | 首片后缺 [2]，续传后 6291739 B（期望 6291739） |
| 分片幂等 | 重传同片 HTTP 200，idempotent=True，bytes_done 仍为 4194304 |
| 错摘要分片 | HTTP 400，code=digest_mismatch |
| 封存重试 | file_id 相同=True，job_id 相同=True，accepted=False |
| UI | 200 行渲染，`服务端 · f_a64ca7492df51efd`，页面错误 0 |

## 用例

### upload · PASS（37.2 s）

```json
{
  "ok": true,
  "session": {
    "parts": 2,
    "bytes_done": 6291739,
    "parts_expected": 2
  },
  "seal": {
    "state": "sealed",
    "bytes": 6291739,
    "content_sig": "9ef8628c22727a60"
  },
  "file_id": "f_854b7bb1f4429d2b",
  "job": {
    "id": "j_b4bd83193a09f9b1f40c6eb7bf63f558",
    "states": [
      "indexing",
      "indexing",
      "indexing",
      "indexed"
    ],
    "seconds": 37.2
  },
  "upload_seconds": 0.05,
  "mb_per_s": 120.01,
  "input": {
    "bytes": 6291739,
    "lines": 48470,
    "sha256": "9ef8628c22727a6053d9634e6276fd2d78fca7e9544fa2f1d92ae43be0db2e63",
    "file": "generated (deterministic templates)"
  },
  "seconds": 37.25
}
```

### query · PASS（1.9 s）

```json
{
  "ok": true,
  "query_id": "q-de0f0c22dd6139df",
  "state": "done",
  "count": {
    "matched": 48470,
    "returned": 48470,
    "relation": "exact"
  },
  "coverage": {
    "scanned_bytes": "6291739",
    "total_bytes": "6291739",
    "complete": true,
    "evaluated_bytes": "6291739",
    "scanned_records": 48479,
    "mode": "postings"
  },
  "plan": "postings",
  "paged": 100,
  "export": {
    "status": 200,
    "lines": 5,
    "rows_header": "5",
    "truncated": "true"
  },
  "narrow": {
    "query_id": "q-3fea6daf7b5c98cc",
    "text": "private-upload-failure",
    "count": {
      "matched": 9,
      "returned": 9,
      "relation": "exact"
    },
    "plan": "postings",
    "hit_ordinals": [
      5000,
      10001,
      15002,
      20003,
      25004
    ],
    "sample": {
      "ordinal": 5000,
      "level": "ERROR",
      "message": "Traceback (most recent call last):",
      "lines": {
        "first": 5001,
        "last": 5003
      },
      "text_has_term": true,
      "text_bytes": 82
    },
    "ok": true
  },
  "seconds": 1.93
}
```

### isolation · PASS（0.0 s）

```json
{
  "ok": true,
  "checks": [
    {
      "target": "/api/v2/files/f_854b7bb1f4429d2b",
      "status": 404,
      "code": "NOT_FOUND"
    },
    {
      "target": "/api/v2/files/f_854b7bb1f4429d2b/records",
      "status": 404,
      "code": "NOT_FOUND"
    },
    {
      "target": "/api/v2/records/f_854b7bb1f4429d2b:0",
      "status": 404,
      "code": "NOT_FOUND"
    },
    {
      "target": "/api/v2/queries/q-de0f0c22dd6139df",
      "status": 404,
      "code": "unknown_query"
    },
    {
      "target": "/api/v2/queries/q-de0f0c22dd6139df/export",
      "status": 404,
      "code": "unknown_query"
    }
  ],
  "peer": "10.9.9.9",
  "note": "身份由 socket 对端推导；同一进程无法portably 绑定两个源地址，故用 handle() 传入另一个对端",
  "seconds": 0.01
}
```

### resume · PASS（0.1 s）

```json
{
  "ok": true,
  "idempotent_part": {
    "status": 200,
    "idempotent": true,
    "bytes_done": 4194304
  },
  "bad_digest": {
    "status": 400,
    "code": "digest_mismatch"
  },
  "missing_after_first_part": [
    2
  ],
  "bytes_done_after_resume": 6291739,
  "expected_bytes": 6291739,
  "seal": {
    "file_id": "f_84b7629c2b26d842",
    "job_id": "j_940d6cf30781c1d4b046691fbb96099a"
  },
  "seal_retry": {
    "file_id": "f_84b7629c2b26d842",
    "job_id": "j_940d6cf30781c1d4b046691fbb96099a",
    "accepted": false,
    "index_state": "framing"
  },
  "seconds": 0.07
}
```

### ui · PASS（76.9 s）

```json
{
  "ok": true,
  "panel_hidden_initially": true,
  "panel_visible_after_click": true,
  "opt_in_unchecked": true,
  "file_input_disabled_before_opt_in": true,
  "file_input_enabled_after_opt_in": true,
  "status_text": "已就绪，可打开服务端视图",
  "rows_rendered": 200,
  "view_label": "服务端 · f_a64ca7492df51efd",
  "page_errors": [],
  "seconds": 76.92
}
```

## 证据

每个用例的原始 JSON 在 `qa/evidence/M4-gate/`；本文件由脚本生成。

## 未覆盖 / 已知限制

- 第二个 principal 通过 `Application.handle(peer=...)` 传入，而不是真的从另一个源地址连接：同一进程无法可移植地绑定第二个回环地址。HTTP 层其余用例都是真实 socket。
- 上传分片为 4 MiB，未在本机跑到 1 GiB；1 GiB 的索引吞吐见 M3 门禁。
- 导出走 `SpooledTemporaryFile` 落到临时磁盘再流式发送，未做真正的边查边发。
- 页面 CSP 为 `script-src 'self'`：`wait_for_function` 的谓词必须是函数表达式，裸表达式会被页面内的 `eval` 拒绝。
