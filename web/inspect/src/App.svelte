<script>
  import { onMount } from 'svelte'
  import { getQueue, getFiling } from './api.js'
  import FilingPane from './FilingPane.svelte'
  import VerdictForm from './VerdictForm.svelte'

  let meta = $state({ year: null, sample: null, seed: null, count: 0, residual: null })
  let items = $state([])
  let selectedId = $state(null)
  let filing = $state(null)
  let loadingFiling = $state(false)

  const labelledCount = $derived(items.filter((i) => i.labelled).length)

  async function refreshQueue() {
    const q = await getQueue()
    meta = {
      year: q.year,
      sample: q.sample,
      seed: q.seed,
      count: q.count,
      residual: q.residual,
    }
    items = q.items
    if (!selectedId && items.length) {
      select((items.find((i) => !i.labelled) || items[0]).doc_id)
    }
  }

  async function select(docId) {
    selectedId = docId
    loadingFiling = true
    filing = await getFiling(docId)
    loadingFiling = false
  }

  function onSaved() {
    // Mark the current filing reviewed (and no longer stale), advance to the
    // next unreviewed one. The server holds the source of truth; this keeps the
    // queue responsive without a round trip.
    items = items.map((i) =>
      i.doc_id === selectedId ? { ...i, labelled: true, stale: false } : i,
    )
    const next = items.find((i) => !i.labelled)
    if (next) select(next.doc_id)
  }

  onMount(refreshQueue)
</script>

<div class="app">
  <header class="bar">
    <h1>openhouse <strong>inspect</strong></h1>
    <span class="meta">{meta.year} · sample {meta.sample} · seed {meta.seed}</span>
    {#if meta.residual}
      <span class="meta">{meta.residual.not_reviewable} not reviewable</span>
    {/if}
    <span class="progress">{labelledCount} / {meta.count} reviewed</span>
  </header>

  <div class="workspace">
    <div class="col queue">
      {#each items as it (it.doc_id)}
        <button
          class="queue-item"
          class:active={it.doc_id === selectedId}
          onclick={() => select(it.doc_id)}
        >
          <div class="who">{it.filer?.first ?? ''} {it.filer?.last ?? ''}</div>
          <div class="sub">
            <span>{it.stratum}</span>
            {#if it.labelled}<span class="tag done">done</span>{/if}
            {#if it.stale}<span class="tag stale">stale</span>{/if}
          </div>
        </button>
      {/each}
    </div>

    <div class="col pdf-pane">
      {#if filing}
        <iframe title="source PDF" src={filing.pdf_url}></iframe>
      {:else}
        <div class="empty">Select a filing to review.</div>
      {/if}
    </div>

    <div class="col">
      {#if loadingFiling}
        <div class="empty">Loading…</div>
      {:else if filing}
        <FilingPane {filing} />
        <VerdictForm docId={filing.doc_id} existing={filing.verdict} onsaved={onSaved} />
      {:else}
        <div class="empty">No filing selected.</div>
      {/if}
    </div>
  </div>
</div>
