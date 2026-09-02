# docx-to-md

Converts a DOCX file to Markdown format.

## When to use

Use this skill when the user asks to extract, convert, or transform a DOCX/Word document into Markdown.

## Instructions

When a user requests to convert a DOCX file to Markdown:

1. Identify the input DOCX file path (absolute, within the workspace)
2. Decide the output Markdown file path (absolute, within the workspace)
3. Call the conversion script with absolute paths:
   ```
   bash("python skills/docx-to-md/docx_to_md.py /path/to/input.docx /path/to/output.md")
   ```
4. Report the results to the user, including the output path and conversion statistics

### Example usage

```
# Convert a DOCX file to Markdown
bash("python skills/docx-to-md/docx_to_md.py /home/bob/.config/cyborg/harness/docs/report.docx /home/bob/.config/cyborg/harness/docs/report.md")
```
