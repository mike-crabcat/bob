# md-to-docx

Convert Markdown files in the workspace to DOCX documents with robust Microsoft Word and email client compatibility.

## When to use

Use this skill when the user asks to:
- Convert a Markdown file to DOCX format
- Create a Word document from a Markdown file
- Export a .md review or document as .docx
- Transform Markdown editorial content into Microsoft Word format

## Instructions

This skill converts workspace Markdown (.md) files into workspace DOCX (.docx) files using a Python script that creates Microsoft Word-compatible DOCX documents with proper metadata and styling.

### Step-by-step process

1. **Identify the input file** - Determine which Markdown file the user wants to convert (must be in the workspace)

2. **Determine the output path** - Either use the user-specified output path or derive it from the input filename (replace .md with .docx)

3. **Run the conversion script** using `bash()`:
   ```
   bash("python skills/md-to-docx/md_to_docx.py input.md output.docx")
   ```
   - Both paths must be workspace-relative (no absolute paths)
   - The input file must have extension `.md` or `.markdown`
   - The output file must have extension `.docx`

4. **Report results** - After successful conversion, inform the user:
   - The output file path
   - The input file path that was converted
   - Approximate paragraph/element count
   - Offer to attach or share the file if requested and tooling allows

### Supported Markdown features

The converter handles:
- **Headings (# through ######)** → Word heading styles (Heading1-Heading6) with appropriate font sizes and bold formatting
- **Regular paragraphs** → Normal style with Times New Roman 12pt font and proper line spacing
- **Bullet lists (-, *, +)** → Bullet points (• symbol)
- **Numbered lists (1., 2., etc.)** → Numbered list items
- **Bold/italic text** → Markdown markers are stripped (formatting removed for compatibility)
- **Horizontal rules** → Em dash separators (—)
- **Fenced code blocks** → Monospace-style paragraphs
- **Tables** → Not directly supported; tables will be rendered as plain text

### Technical improvements for compatibility

The converter creates robust DOCX files that include:
- **Complete metadata** (`docProps/core.xml`, `docProps/app.xml`) required by Microsoft Word
- **Proper namespace declarations** for XML validation
- **Complete style definitions** with fonts (Calibri for headings, Times New Roman for body), sizes, and spacing
- **Page setup properties** with standard margins (1 inch on all sides)
- **Relationship entries** for all document components
- **Content type registrations** for all parts

This ensures the generated DOCX files:
- Open correctly in Microsoft Word (Windows and Mac)
- Open correctly in LibreOffice Writer
- Open correctly in Google Docs
- Can be emailed as attachments without corruption
- Validate against the Office Open XML standard

### Important notes

- Paths are workspace-relative only - the script will reject absolute paths and path traversal attempts
- The script creates output directories automatically if they don't exist
- Output DOCX files are fully compatible with Microsoft Word, LibreOffice Writer, Google Docs, and other OOXML-compliant editors
- Bold/italic markdown markers are stripped rather than converted to formatting for maximum compatibility
- If the input file doesn't exist or cannot be read, the script provides a clear error message

## Example usage

```python
# Convert a PR review to Word format
bash("python skills/md-to-docx/md_to_docx.py docs/PR-review.md docs/PR-review.docx")

# Convert a Markdown report
bash("python skills/md-to-docx/md_to_docx.py report.md output/report.docx")

# Convert a literary review
bash("python skills/md-to-docx/md_to_docx.py reviews/literary/2026-06-09-review.md reviews/literary/2026-06-09-review.docx")
```

## Limitations

- Tables are not supported in Markdown parsing; table content will be rendered as plain text
- Inline code (backticks) is treated as regular text
- Links are not parsed as hyperlinks; URL text appears as plain text
- Images are not embedded or referenced
- Bold/italic markers are stripped rather than converted to formatting
- Complex nested lists are flattened to single-level lists
- No support for blockquotes, footnotes, or advanced Markdown features

## Testing

The converter has been tested with:
- Multi-page literary reviews with complex headings and lists
- Simple README files with basic formatting
- Files containing code blocks and horizontal rules

Test output should open successfully in:
- Microsoft Word 2019/2021/365
- LibreOffice Writer 7.x
- Google Docs (upload and view)
- Apple Pages
- Apache OpenOffice
