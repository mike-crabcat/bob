# pdf-to-text

**name:** pdf-to-text

**description:** Downloads or accepts a local PDF and extracts readable text. Designed for cases like arXiv PDFs or any linked PDF the user asks to read.

**trigger:** Trigger this skill when the user asks to download, read, extract text from, or summarize a PDF, especially from a URL or arXiv page.

## Instructions

When a user asks you to work with a PDF (download, read, extract text, or summarize):

1. **Invoke the script** using `bash("python skills/pdf-to-text/pdf_to_text.py ...")` with appropriate arguments:
   - **Positional argument:** The PDF URL or local file path (absolute or relative)
   - **Optional arguments:**
     - `--out-dir <directory>`: Output directory (absolute path)
     - `--basename <name>`: Override output filename stem
     - `--no-download`: Treat input as local file only, skip URL handling
     - `--keep-layout`: Preserve layout with `pdftotext -layout` if available

2. **Handle arXiv URLs:** If the user provides an arXiv abs URL like `https://arxiv.org/abs/2504.13171`, the script will automatically resolve it to `https://arxiv.org/pdf/2504.13171.pdf`.

3. **Post-extraction workflow:** After text extraction, if the user asks for summarization or questions about the content:
   - Read the generated `.txt` file using `read_file`
   - Answer questions or summarize based on the extracted text

## Example Usage

```python
# Download from URL and extract to scratch/
bash("python skills/pdf-to-text/pdf_to_text.py https://arxiv.org/abs/2504.13171 --out-dir /home/bob/.config/cyborg/harness/scratch")

# Extract from local workspace PDF
bash("python skills/pdf-to-text/pdf_to_text.py /home/bob/.config/cyborg/harness/docs/paper.pdf --out-dir /home/bob/.config/cyborg/harness/scratch")
```

## Error Handling

The script handles errors gracefully with user-friendly messages.
