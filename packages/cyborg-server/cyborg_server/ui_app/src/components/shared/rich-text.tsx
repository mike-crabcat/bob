import { Link } from "@tanstack/react-router";

const CONTACT_REF_RE = /\{\{contact:([^|}]+)\|([^}]+)\}\}/g;

export function RichText({ text }: { text: string }) {
  const parts: (string | { contactId: string; name: string })[] = [];
  let lastIndex = 0;

  for (const match of text.matchAll(CONTACT_REF_RE)) {
    if (match.index! > lastIndex) {
      parts.push(text.slice(lastIndex, match.index!));
    }
    parts.push({ contactId: match[1], name: match[2] });
    lastIndex = match.index! + match[0].length;
  }
  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }

  if (parts.length === 0 || (parts.length === 1 && typeof parts[0] === "string")) {
    return <>{text}</>;
  }

  return (
    <>
      {parts.map((part, i) =>
        typeof part === "string" ? (
          <span key={i}>{part}</span>
        ) : (
          <Link
            key={i}
            to="/contacts/$contactId"
            params={{ contactId: part.contactId }}
            className="text-accent hover:underline"
          >
            {part.name}
          </Link>
        ),
      )}
    </>
  );
}
