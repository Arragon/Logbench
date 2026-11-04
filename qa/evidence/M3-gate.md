# M3 Gate · 服务端本地索引纵切（R6-041）

- 结论：**PASS**
- 时间：2026-11-03 11:25:11（耗时 516.3 s）
- 用例：5 通过 / 0 失败

## 环境

| 项 | 值 |
| --- | --- |
| python | 3.9.11 |
| python_implementation | CPython |
| platform | macOS-27.0.1-arm64-arm-64bit |
| machine | arm64 |
| cpu_count | 12 |
| node | v22.22.2 |
| sqlite | 3.38.1 |
| threads | 1 |
| disk_free_gb | 214.4 |
| pid | 26989 |

## 命令

```
env -u PYTHONPATH ~/.local/bin/python3.9 qa/r6/server_index_gate.py --size-mb 1024
```

## 关键数字

| 项 | 值 |
| --- | --- |
| 输入 | 1073741840 B / 2049763 行（sha256 `d6a177c26d6fbdaa`，平均 523.8 B/行） |
| 冷解析（结构层） | 143.96 s，7.11 MB/s，1025 个解析批次，2049763 条记录 |
| 规则层重算 | 363.34 s，2049763 行入库（覆盖校验通过），1366509 个时间线节点，解析批次 0 |
| 暖重开 | 结构层复用 True，规则层复用 True，解析请求增量 0，读原文字节 0 |
| 内存峰值 | 进程 412.1 MB，子进程 54.8 MB（Node RSS 采样 0.0 MB） |
| 索引体积 | 4858351616 B（约 4.5× 源文件） |

后台满载时健康接口 p50 1.29 ms / p95 3.5 ms（163 次采样）。

## 用例

### golden · PASS（0.1 s）

```json
{
  "ok": true,
  "worker_counts_identical": true,
  "fixtures": {
    "lanes-1": {
      "edge_cases.log": {
        "records": 10,
        "digest": "e08887af865116c5cd8980850fcda8ef105c82839c1306e83ce6d4ea1e1c1309",
        "expected": 10
      },
      "no_trailing_newline.log": {
        "records": 2,
        "digest": "c123b6b20c575c0c0aff10b61df431fbb5ca939e22faa7a5a827aac63996b0dc",
        "expected": 2
      },
      "traceback_chain.log": {
        "records": 6,
        "digest": "1afd251758b195bb1c11845d7c55b642c35f71c2999499f87170b0a596ed578c",
        "expected": 6
      },
      "unicode_crlf_bom.log": {
        "records": 3,
        "digest": "65c54f805550e70a1e3a2eec93783aa589087a97d280ab783736019eab964e44",
        "expected": 3
      }
    },
    "lanes-4": {
      "edge_cases.log": {
        "records": 10,
        "digest": "e08887af865116c5cd8980850fcda8ef105c82839c1306e83ce6d4ea1e1c1309",
        "expected": 10
      },
      "no_trailing_newline.log": {
        "records": 2,
        "digest": "c123b6b20c575c0c0aff10b61df431fbb5ca939e22faa7a5a827aac63996b0dc",
        "expected": 2
      },
      "traceback_chain.log": {
        "records": 6,
        "digest": "1afd251758b195bb1c11845d7c55b642c35f71c2999499f87170b0a596ed578c",
        "expected": 6
      },
      "unicode_crlf_bom.log": {
        "records": 3,
        "digest": "65c54f805550e70a1e3a2eec93783aa589087a97d280ab783736019eab964e44",
        "expected": 3
      }
    }
  },
  "compare": "counts vs tests/fixtures/r6/golden/expected.json",
  "seconds": 0.11
}
```

### cold_warm · PASS（507.9 s）

```json
{
  "ok": true,
  "input": {
    "bytes": 1073741840,
    "lines": 2049763,
    "sha256": "d6a177c26d6fbdaaa4fcbcaf73463c2f3a6dbf8a2e38d4b04a75c390d617941b",
    "avg_line_bytes": 523.8,
    "path": "generated (deterministic templates)"
  },
  "cold": {
    "seconds": 143.96,
    "mb_per_s": 7.11,
    "parse_requests": 1025,
    "records": 2049763,
    "source_bytes": 1073741840
  },
  "rules": {
    "seconds": 363.34,
    "records": 2049763,
    "stored_rows": 2049763,
    "timeline_nodes": 1366509,
    "source_bytes_read": 1073741840,
    "parse_requests": 0
  },
  "warm": {
    "reused": true,
    "rules_reused": true,
    "parse_requests_delta": 0,
    "source_bytes_read": 0
  },
  "memory": {
    "self_peak_mb": 412.1,
    "children_peak_mb": 54.8,
    "peak_node_mb": 0.0,
    "samples": 1896
  },
  "total_seconds": 507.68,
  "index_bytes": 4858351616,
  "seconds": 507.85
}
```

### resilience · PASS（1.5 s）

```json
{
  "ok": true,
  "cases": {
    "node_crash": {
      "ok": true,
      "killed": {
        "at": null,
        "pid": 23620,
        "returncode": -9
      },
      "partial_records": 3069,
      "resume_from": 393216,
      "records": 2000,
      "replayed_equal": true
    },
    "stale_fencing": {
      "ok": true,
      "result": "stale_fencing",
      "first": 1,
      "second": 2
    },
    "sqlite_failure": {
      "ok": true,
      "raised": "ProgrammingError",
      "note": "a failed batch commits neither records nor watermark"
    },
    "post_publish_kill": {
      "ok": true,
      "versions": [],
      "note": "publication commits the pointer before the caller can be killed"
    }
  },
  "seconds": 1.48
}
```

### budget · PASS（6.7 s）

```json
{
  "ok": true,
  "health_p50_ms": 1.29,
  "health_p95_ms": 3.5,
  "samples": 163,
  "errors": [],
  "pool": {
    "spawned": 1,
    "killed": 0,
    "restarted": 0,
    "requests": 774,
    "payload_bytes": 67157616
  },
  "budget": {
    "parse_slots": 4,
    "as_dict": null
  },
  "memory": {
    "self_peak_mb": 412.1,
    "children_peak_mb": 204.5
  },
  "seconds": 6.74
}
```

### read_api · PASS（0.1 s）

```json
{
  "ok": true,
  "raw_matches_disk": true,
  "giant_body": {
    "truncated": true,
    "total": 6002,
    "returned": 1383,
    "next": 1383,
    "note": "served by cursor, not silently clipped"
  },
  "foreign_status": 404,
  "timeline": {
    "nodes": 1,
    "relation": "exact",
    "coverage": "complete"
  },
  "seconds": 0.13
}
```

## 证据文件

每个用例的原始 JSON 在 `qa/evidence/M3-gate/`，本文件的数字由脚本生成，不是手写。

## 未覆盖 / 已知限制

- 本机为 macOS（macOS-27.0.1-arm64-arm-64bit），未在 Linux 目标机复跑；SQLite 版本见上表（目标机 3.38 亦不支持 WAL 安全断言）。
- 1 GiB 用例使用确定性模板生成，sha256 已记录，可复现但非真实业务日志；平均行宽 523.8 B，与短行日志相比记录数更少、单条更大。
- Node 峰值 RSS 采样依赖 `ps`，本机沙箱内不可用，故该项为 0（B-024）；内存证据以 `getrusage` 为准。
- 索引体积约为源文件的 4–5 倍（B-025），未做分片压缩与旧代次行回收。
- 规则层重算需要重读原文（条件可指向 `raw`），`source_bytes_read` 如实记录为整个文件。
- 远程采集（M4）不在本门禁范围。
