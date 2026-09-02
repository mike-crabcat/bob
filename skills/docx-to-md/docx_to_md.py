#!/usr/bin/env python3
"""
Convert DOCX files to Markdown format.

This script reads a DOCX file (which is a ZIP containing XML) and converts
it to Markdown format. It preserves headings, lists, and simple tables.

Usage: python docx_to_md.py <input.docx> <output.md>
"""

import argparse
import os
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

# DOCX XML namespaces
NAMESPACES = {
    'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
}


def is_safe_path(path_str):
    """Check if a path doesn't contain traversal patterns."""
    if '..' in Path(path_str).parts:
        return False
    return True


def validate_input_path(input_path):
    """Validate the input DOCX file path."""
    if not is_safe_path(input_path):
        raise ValueError(f"Input path contains unsafe traversal: {input_path}")

    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")

    if not os.path.isfile(input_path):
        raise ValueError(f"Input path is not a file: {input_path}")

    if not input_path.lower().endswith('.docx'):
        raise ValueError(f"Input file must have .docx extension: {input_path}")

    # Verify it's a valid ZIP file (DOCX is a ZIP archive)
    if not zipfile.is_zipfile(input_path):
        raise ValueError(f"Input file is not a valid DOCX file (not a ZIP archive): {input_path}")

    return True


def validate_output_path(output_path):
    """Validate the output Markdown file path."""
    if not is_safe_path(output_path):
        raise ValueError(f"Output path contains unsafe traversal: {output_path}")

    if not (output_path.lower().endswith('.md') or output_path.lower().endswith('.markdown')):
        raise ValueError(f"Output file must have .md or .markdown extension: {output_path}")

    return True


def extract_text_from_element(element):
    """Extract text content from a Word XML element, handling text runs and formatting."""
    texts = []

    # Find all text runs (w:t)
    for text_elem in element.findall('.//w:t', NAMESPACES):
        if text_elem.text:
            texts.append(text_elem.text)

    # Find tab characters
    for tab_elem in element.findall('.//w:tab', NAMESPACES):
        texts.append('\t')

    # Find line breaks
    for br_elem in element.findall('.//w:br', NAMESPACES):
        texts.append('\n')

    return ''.join(texts)


def get_heading_level(p_elem):
    """Extract heading level from paragraph style."""
    # Try to get style from w:pPr/w:pStyle
    ppr = p_elem.find('w:pPr', NAMESPACES)
    if ppr is not None:
        style_elem = ppr.find('w:pStyle', NAMESPACES)
        if style_elem is not None:
            style_val = style_elem.get(f'{{{NAMESPACES["w"]}}}val', '')
            style_val = style_val.lower()

            # Word uses styles like "Heading1", "Heading2", etc.
            if 'heading' in style_val or 'title' in style_val:
                if 'heading1' in style_val or 'title' in style_val:
                    return 1
                elif 'heading2' in style_val:
                    return 2
                elif 'heading3' in style_val:
                    return 3
                elif 'heading4' in style_val:
                    return 4
                elif 'heading5' in style_val:
                    return 5
                elif 'heading6' in style_val:
                    return 6

    return None


def is_list_item(p_elem):
    """Check if paragraph is a list item."""
    ppr = p_elem.find('w:pPr', NAMESPACES)
    if ppr is not None:
        # Check for numbering
        num_pr = ppr.find('w:numPr', NAMESPACES)
        if num_pr is not None:
            return True

        # Check for bullets
        ppr_val = ppr.find('w:pStyle', NAMESPACES)
        if ppr_val is not None:
            style_val = ppr_val.get(f'{{{NAMESPACES["w"]}}}val', '')
            if 'list' in style_val.lower() or 'bullet' in style_val.lower():
                return True

    return False


def extract_table_rows(table_elem):
    """Extract rows from a table element."""
    rows = []
    for tr in table_elem.findall('.//w:tr', NAMESPACES):
        cells = []
        for tc in tr.findall('.//w:tc', NAMESPACES):
            cell_text = extract_text_from_element(tc).strip()
            cells.append(cell_text)
        if cells:  # Only add non-empty rows
            rows.append(cells)
    return rows


def table_to_markdown(rows):
    """Convert table rows to Markdown table format."""
    if not rows or len(rows) < 1:
        return ""

    # Use first row as header
    header = rows[0]
    data_rows = rows[1:] if len(rows) > 1 else []

    # Create header row
    md_lines = []
    md_lines.append('| ' + ' | '.join(header) + ' |')

    # Create separator row
    if header:
        md_lines.append('|' + '|'.join([' --- ' for _ in header]) + '|')

    # Add data rows
    for row in data_rows:
        # Ensure all rows have same number of cells as header
        while len(row) < len(header):
            row.append('')
        md_lines.append('| ' + ' | '.join(row[:len(header)]) + ' |')

    return '\n'.join(md_lines)


def convert_docx_to_markdown(docx_path):
    """Convert DOCX file to Markdown text."""
    # Open the DOCX file (it's a ZIP)
    with zipfile.ZipFile(docx_path, 'r') as zip_file:
        # Read the main document
        document_xml = zip_file.read('word/document.xml')

    # Parse XML
    root = ET.fromstring(document_xml)

    # Find the body element
    body = root.find('.//w:body', NAMESPACES)
    if body is None:
        return ""

    markdown_lines = []
    paragraph_count = 0
    table_count = 0

    # Process all paragraphs and tables in order
    for elem in body:
        if elem.tag == f'{{{NAMESPACES["w"]}}}p':
            # It's a paragraph
            paragraph_count += 1
            text = extract_text_from_element(elem).strip()

            if not text:
                # Empty paragraph - add blank line
                markdown_lines.append('')
                continue

            # Check for heading
            heading_level = get_heading_level(elem)
            if heading_level:
                # Convert to heading
                prefix = '#' * heading_level
                markdown_lines.append(f"{prefix} {text}")
            elif is_list_item(elem):
                # Convert to bullet list item
                markdown_lines.append(f"- {text}")
            else:
                # Regular paragraph
                markdown_lines.append(text)

        elif elem.tag == f'{{{NAMESPACES["w"]}}}tbl':
            # It's a table
            table_count += 1
            rows = extract_table_rows(elem)
            if rows:
                table_md = table_to_markdown(rows)
                markdown_lines.append(table_md)
                markdown_lines.append('')  # Blank line after table

    # Join lines with proper spacing
    result = '\n'.join(markdown_lines)

    return result, paragraph_count, table_count


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Convert DOCX files to Markdown format.',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('input_docx_path', help='Path to input .docx file')
    parser.add_argument('output_md_path', help='Path to output .md file')

    args = parser.parse_args()

    try:
        # Validate paths
        validate_input_path(args.input_docx_path)
        validate_output_path(args.output_md_path)

        # Create parent directories for output if needed
        output_dir = os.path.dirname(args.output_md_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        # Convert DOCX to Markdown
        markdown_text, para_count, table_count = convert_docx_to_markdown(args.input_docx_path)

        # Write output
        with open(args.output_md_path, 'w', encoding='utf-8') as f:
            f.write(markdown_text)

        # Print success summary
        print(f"✓ Successfully converted {args.input_docx_path} to {args.output_md_path}")
        print(f"  - {para_count} paragraphs processed")
        print(f"  - {table_count} tables converted")
        print(f"  - Output file size: {len(markdown_text)} bytes")

        return 0

    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: Failed to convert DOCX file - {e}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
