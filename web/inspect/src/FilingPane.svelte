<script>
  // The right-hand reference column: the parsed record (what the parser produced),
  // rendered as labeled metadata + tables so the reviewer can judge precision and
  // recall against the embedded PDF beside it without reading raw JSON.
  import RecordTable from './RecordTable.svelte'

  let { filing } = $props()

  const meta = $derived(filing.filing ?? {})

  // Labeled metadata rows, in reading order. Each is [label, value]; nulls and
  // absent fields render as "—" rather than vanishing (never silently drop).
  const metaRows = $derived.by(() => {
    const f = meta.filer ?? {}
    const name = [f.prefix, f.first, f.last, f.suffix].filter(Boolean).join(' ')
    const sd = meta.state_district
    const ft = meta.filing_type ?? {}
    return [
      ['Filer', name || null],
      ['Filer ID', meta.filer_id],
      ['State / district', sd ? sd.raw : null],
      ['Type', ft.label ? `${ft.label}${ft.code ? ` (${ft.code})` : ''}` : null],
      ['Filing date', meta.filing_date],
      ['PDF class', meta.pdf_class],
      ['Parse status', meta.parse_status],
      ['Bioguide ID', meta.bioguide_id],
      ['Doc ID', meta.doc_id],
      ['Year', meta.year],
      ['Source PDF', meta.source_pdf],
    ]
  })

  // The parsed body is a PTR (a transactions array) or an FD (a dict of
  // schedules keyed by letter). Branch on whichever key is present; neither
  // means there's no parsed body to show (e.g. a failed extraction).
  const transactions = $derived(filing.body?.transactions ?? null)
  const schedules = $derived(filing.body?.schedules ?? null)
</script>

<div class="record">
  {#if filing.stale}
    <div class="banner">
      This filing changed since it was last reviewed (snapshot mismatch) —
      re-check it and save again.
    </div>
  {/if}

  <h2>Filing</h2>
  <dl class="fields">
    {#each metaRows as [label, value]}
      <dt>{label}</dt>
      <dd class:muted={value == null || value === ''}>{value ?? '—'}</dd>
    {/each}
  </dl>

  {#if transactions}
    <h2>Transactions ({transactions.length})</h2>
    {#if transactions.length}
      <RecordTable rows={transactions} />
    {:else}
      <p class="empty">No transactions in the parsed record.</p>
    {/if}
  {:else if schedules}
    {#each Object.entries(schedules) as [letter, rows]}
      <h2>Schedule {letter} ({rows.length})</h2>
      <RecordTable {rows} />
    {/each}
  {:else}
    <p class="empty">
      No parsed body for this filing — see parse status above.
    </p>
  {/if}
</div>
