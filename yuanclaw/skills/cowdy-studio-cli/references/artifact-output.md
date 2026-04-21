# Studio Artifact Output

Open this file only after the machine action is complete and Studio should render the result through an existing visualization component.

## Core rule

Normal conversation stays plain text. Use a fenced ` ```<module>` JSON block only when Studio should render a structured card, table, progress view, confirmation, image gallery, or file preview.

## Common envelope

````markdown
```<module>
{
  "type": "file",
  "content": "已生成《PSPS模型.md》，可直接预览。",
  "payload": {
    "fileName": "PSPS模型.md",
    "fileType": "Markdown 文档",
    "sourcePath": "docs/PSPS模型.md",
    "previewLanguage": "markdown",
    "previewContent": "# PSPS 模型\n\n## 当前判断\n- ..."
  }
}
```
````

Rules:

- One block is one Studio-renderable message.
- JSON only. No comments or trailing commas.
- Keep long explanation outside the JSON block.
- `type` must be one of `form`, `progress`, `table`, `card`, `image`, `confirm`, `file`.

## Module selection

- `form`
  - collect structured missing input
- `progress`
  - show stage, step, or milestone progress
- `table`
  - show tabular mappings or matrices
- `card`
  - use `payload.type = "link-card"` or `payload.type = "deploy-card"`
- `image`
  - show multiple candidate images or a chosen image set
- `confirm`
  - ask for explicit approval before a risky action
- `file`
  - mount a deliverable artifact into Studio preview

## File module rule

Prefer a `file` module over plain prose like “已生成 xx.md”.

When content is available, include preview hints:

```json
{
  "type": "file",
  "content": "文档已生成。",
  "payload": {
    "fileName": "数据表设计逻辑.md",
    "fileType": "Markdown 文档",
    "downloadUrl": "docs/engineering/specs/数据表设计逻辑.md",
    "previewUrl": "docs/engineering/specs/数据表设计逻辑.md",
    "sourcePath": "docs/engineering/specs/数据表设计逻辑.md",
    "previewLanguage": "markdown",
    "previewContent": "# 数据表设计逻辑\n\n## 表一览\n- ..."
  }
}
```

- For `.md`, use `previewLanguage: "markdown"`.
- For `.json`, `.yaml`, `.txt`, `.csv`, use the matching preview language when known.
- For `.pdf` and `.docx`, still emit the `file` module even when inline preview content is unavailable.

## Current visualization mapping

| module contract | Studio visualization |
|---|---|
| `form` | `intake-form` |
| `progress` | `progress-board` |
| `table` | `data-table` |
| `card` + `link-card` | `resource-link-card` |
| `card` + `deploy-card` | `summary-card` |
| `image` | `image-gallery` |
| `confirm` | `confirmation-card` |
| `file` | `delivery-file-card` |
| `file` + `.md` | `markdown-preview` |
| `file` + `.pdf` | `pdf-preview` |
| `file` + `.doc/.docx` | `docx-preview` |

## What not to do

- Do not use module blocks as the control plane for doing the work.
- Do not emit markdown tables when a Studio `table` should render.
- Do not invent new card variants beyond `link-card` and `deploy-card`.
- Do not mention an artifact only in prose when Studio should preview it.
