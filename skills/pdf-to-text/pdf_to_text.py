#!/usr/bin/env python3
"""
PDF to Text Extraction Tool

Downloads or reads a PDF and extracts text to the specified output directory.
Supports URLs (including arXiv) and local files.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.error import URLError, HTTPError


def sanitize_basename(name: str) -> str:
    """Sanitize filename by removing/replacing dangerous characters."""
    # Remove path separators
    name = os.path.basename(name)
    # Replace problematic characters with underscore
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    # Remove leading/trailing dots and spaces
    name = name.strip('. ')
    # Limit length
    if len(name) > 200:
        name = name[:200]
    return name or "output"


def resolve_arxiv_url(url: str) -> str:
    """Convert arXiv abs URL to PDF URL."""
    arxiv_abs_pattern = r'https?://arxiv\.org/abs/(\d+\.\d+)'
    match = re.match(arxiv_abs_pattern, url)
    if match:
        paper_id = match.group(1)
        return f"https://arxiv.org/pdf/{paper_id}.pdf"
    return url


def download_pdf(url: str, output_path: Path) -> bool:
    """Download PDF from URL to output path."""
    try:
        # Create output directory if it doesn't exist
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Download with user agent
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; PDFExtractor/1.0)'}
        request = urllib.request.Request(url, headers=headers)

        with urllib.request.urlopen(request, timeout=30) as response:
            with open(output_path, 'wb') as f:
                f.write(response.read())
        return True
    except (URLError, HTTPError) as e:
        print(f"Error: Failed to download PDF from {url}", file=sys.stderr)
        print(f"Reason: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Error: Unexpected error during download: {e}", file=sys.stderr)
        return False


def extract_text_pdftotext(pdf_path: Path, txt_path: Path, keep_layout: bool) -> bool:
    """Extract text using system pdftotext command."""
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        return False

    try:
        cmd = [pdftotext]
        if keep_layout:
            cmd.append("-layout")
        cmd.extend([str(pdf_path), str(txt_path)])

        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=60,
            check=True
        )

        # Verify output was created
        if txt_path.exists() and txt_path.stat().st_size > 0:
            return True
        return False
    except subprocess.TimeoutExpired:
        print(f"Error: pdftotext timed out", file=sys.stderr)
        return False
    except subprocess.CalledProcessError as e:
        print(f"Error: pdftotext failed with code {e.returncode}", file=sys.stderr)
        if e.stderr:
            print(f"Details: {e.stderr.decode()}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Error: pdftotext extraction failed: {e}", file=sys.stderr)
        return False


def extract_text_pypdf(pdf_path: Path, txt_path: Path) -> bool:
    """Extract text using pypdf library."""
    try:
        import pypdf
    except ImportError:
        return False

    try:
        with open(pdf_path, 'rb') as f:
            reader = pypdf.PdfReader(f)
            text = []

            for page in reader.pages:
                try:
                    page_text = page.extract_text()
                    if page_text.strip():
                        text.append(page_text)
                except Exception:
                    continue

            # Write output
            txt_path.parent.mkdir(parents=True, exist_ok=True)
            with open(txt_path, 'w', encoding='utf-8') as out:
                out.write('\n\n'.join(text))

            return txt_path.exists() and txt_path.stat().st_size > 0
    except Exception as e:
        print(f"Error: pypdf extraction failed: {e}", file=sys.stderr)
        return False


def extract_text_pypdf2(pdf_path: Path, txt_path: Path) -> bool:
    """Extract text using PyPDF2 library."""
    try:
        import PyPDF2
    except ImportError:
        return False

    try:
        with open(pdf_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            text = []

            for page in reader.pages:
                try:
                    page_text = page.extract_text()
                    if page_text.strip():
                        text.append(page_text)
                except Exception:
                    continue

            # Write output
            txt_path.parent.mkdir(parents=True, exist_ok=True)
            with open(txt_path, 'w', encoding='utf-8') as out:
                out.write('\n\n'.join(text))

            return txt_path.exists() and txt_path.stat().st_size > 0
    except Exception as e:
        print(f"Error: PyPDF2 extraction failed: {e}", file=sys.stderr)
        return False


def get_pdf_page_count(pdf_path: Path) -> int:
    """Get page count from PDF if possible."""
    # Try pdftotext -info
    pdftotext = shutil.which("pdftotext")
    if pdftotext:
        try:
            result = subprocess.run(
                [pdftotext, "-info", str(pdf_path)],
                capture_output=True,
                timeout=10,
                text=True
            )
            # Parse output for page count
            match = re.search(r'Pages:\s*(\d+)', result.stdout)
            if match:
                return int(match.group(1))
        except Exception:
            pass

    # Try pypdf
    try:
        import pypdf
        with open(pdf_path, 'rb') as f:
            reader = pypdf.PdfReader(f)
            return len(reader.pages)
    except Exception:
        pass

    # Try PyPDF2
    try:
        import PyPDF2
        with open(pdf_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            return len(reader.pages)
    except Exception:
        pass

    return 0


def main():
    parser = argparse.ArgumentParser(
        description='Extract text from PDF files',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  %(prog)s https://arxiv.org/abs/2504.13171
  %(prog)s docs/paper.pdf --out-dir extracted/
  %(prog)s https://example.com/paper.pdf --keep-layout
        '''
    )

    parser.add_argument(
        'input',
        help='PDF URL or local file path'
    )

    parser.add_argument(
        '--out-dir',
        default='scratch',
        help='Output directory for extracted text (default: scratch)'
    )

    parser.add_argument(
        '--basename',
        help='Override output filename stem'
    )

    parser.add_argument(
        '--no-download',
        action='store_true',
        help='Treat input as local file only, skip URL handling'
    )

    parser.add_argument(
        '--keep-layout',
        action='store_true',
        help='Preserve layout using pdftotext -layout if available'
    )

    args = parser.parse_args()

    # Determine workspace root — prefer env var, fall back to cwd
    workspace_root = Path(os.environ.get("CYBORG_WORKSPACE_DIR", str(Path.cwd())))

    # Setup output directory
    out_dir = workspace_root / args.out_dir
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"Error: Cannot create output directory {out_dir}: {e}", file=sys.stderr)
        sys.exit(1)

    # Process input
    input_arg = args.input
    pdf_path = None
    downloaded = False
    source_input = input_arg
    resolved_url = None

    # Check if input is a URL
    is_url = input_arg.startswith('http://') or input_arg.startswith('https://')

    if is_url and not args.no_download:
        # Resolve arXiv URLs
        resolved_url = resolve_arxiv_url(input_arg)

        # Determine basename
        if args.basename:
            basename = sanitize_basename(args.basename)
        else:
            # Extract filename from URL
            url_path = Path(urllib.parse.urlparse(resolved_url).path)
            basename = sanitize_basename(url_path.stem)

        # Download PDF
        pdf_filename = f"{basename}.pdf"
        pdf_path = out_dir / pdf_filename

        print(f"Downloading PDF from: {resolved_url}")
        if not download_pdf(resolved_url, pdf_path):
            sys.exit(1)

        downloaded = True
        print(f"Downloaded to: {pdf_path}")
    else:
        # Treat as local file
        # Check if it's an absolute path or workspace-relative
        input_path = Path(input_arg)
        if input_path.is_absolute():
            pdf_path = input_path
        else:
            pdf_path = workspace_root / input_arg

        # Verify file exists
        if not pdf_path.exists():
            print(f"Error: File not found: {pdf_path}", file=sys.stderr)
            sys.exit(1)

        if not pdf_path.is_file():
            print(f"Error: Not a file: {pdf_path}", file=sys.stderr)
            sys.exit(1)

        # Determine basename
        if args.basename:
            basename = sanitize_basename(args.basename)
        else:
            basename = sanitize_basename(pdf_path.stem)

    # Extract text
    txt_filename = f"{basename}.txt"
    txt_path = out_dir / txt_filename

    print(f"Extracting text from: {pdf_path}")

    # Try extraction methods in order of preference
    success = False

    # 1. Try pdftotext
    if shutil.which("pdftotext"):
        print("Using pdftotext for extraction...")
        success = extract_text_pdftotext(pdf_path, txt_path, args.keep_layout)

    # 2. Try pypdf
    if not success:
        print("pdftotext not available or failed, trying pypdf...")
        success = extract_text_pypdf(pdf_path, txt_path)

    # 3. Try PyPDF2
    if not success:
        print("pypdf not available or failed, trying PyPDF2...")
        success = extract_text_pypdf2(pdf_path, txt_path)

    if not success:
        print("Error: All text extraction methods failed", file=sys.stderr)
        print("Hint: Install pdftotext (poppler-utils) for best results:", file=sys.stderr)
        print("  Ubuntu/Debian: sudo apt-get install poppler-utils", file=sys.stderr)
        print("  macOS: brew install poppler", file=sys.stderr)
        print("  Or install pypdf: pip install pypdf", file=sys.stderr)
        sys.exit(1)

    # Get stats
    page_count = get_pdf_page_count(pdf_path)
    char_count = txt_path.stat().st_size

    # Success output
    print()
    print("✓ Text extraction successful")
    print(f"  Source: {source_input}")
    if resolved_url and resolved_url != source_input:
        print(f"  Resolved URL: {resolved_url}")
    if downloaded:
        print(f"  PDF saved to: {pdf_path}")
    print(f"  Text saved to: {txt_path}")
    if page_count > 0:
        print(f"  Pages: {page_count}")
    print(f"  Characters: {char_count:,}")


if __name__ == '__main__':
    main()
