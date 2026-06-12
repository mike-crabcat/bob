const base = import.meta.env.BASE_URL.replace(/\/$/, "");

function getSecret(): string {
  const m = document.cookie.match(/bob_dashboard_secret=([^;]+)/);
  return m ? m[1] : "";
}

export async function fetchAPI<T>(path: string): Promise<T> {
  const secret = getSecret();
  const sep = path.includes("?") ? "&" : "?";
  const url = `${base}/api${path}${secret ? `${sep}secret=${encodeURIComponent(secret)}` : ""}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`API ${res.status}: ${res.statusText}`);
  return res.json();
}

export async function postAPI<T>(path: string, body: unknown): Promise<T> {
  const secret = getSecret();
  const sep = path.includes("?") ? "&" : "?";
  const url = `${base}/api${path}${secret ? `${sep}secret=${encodeURIComponent(secret)}` : ""}`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`API ${res.status}: ${res.statusText}`);
  return res.json();
}

export async function putAPI<T>(path: string, body: unknown): Promise<T> {
  const secret = getSecret();
  const sep = path.includes("?") ? "&" : "?";
  const url = `${base}/api${path}${secret ? `${sep}secret=${encodeURIComponent(secret)}` : ""}`;
  const res = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`API ${res.status}: ${res.statusText}`);
  return res.json();
}
