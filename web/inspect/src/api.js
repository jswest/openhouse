// Thin wrappers over the inspect server's JSON API. Relative URLs: the same
// stdlib http.server that serves this bundle also serves /api, so no base URL.

export async function getQueue() {
  const r = await fetch('/api/queue')
  return r.json()
}

export async function getFiling(docId) {
  const r = await fetch(`/api/filing/${encodeURIComponent(docId)}`)
  return r.ok ? r.json() : null
}

export async function postVerdict(docId, payload) {
  const r = await fetch(`/api/verdict/${encodeURIComponent(docId)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  const data = await r.json()
  if (!r.ok) throw new Error(data.error || 'verdict rejected')
  return data
}
