<script>
  // Renders an array of parsed records (PTR transactions or one FD schedule's
  // rows) as a table. Columns are the union of keys across the rows, so it adapts
  // to whatever fields a given schedule carries without hardcoding ten layouts.
  // The verbatim `raw_text` each row preserves is kept out of the grid (it's long)
  // but surfaced per-row behind a toggle — shown, never dropped.
  let { rows } = $props()

  const HIDE = new Set(['raw_text'])

  const columns = $derived.by(() => {
    const seen = []
    for (const row of rows) {
      for (const key of Object.keys(row ?? {})) {
        if (!HIDE.has(key) && !seen.includes(key)) seen.push(key)
      }
    }
    return seen
  })

  const hasRaw = $derived(rows.some((r) => r?.raw_text))

  // Amount ranges and similar carry a human `label`; show it. Other objects get
  // their non-null values joined. Scalars pass through; null/blank → "—".
  function fmt(value) {
    if (value == null || value === '') return '—'
    if (typeof value === 'boolean') return value ? 'yes' : 'no'
    if (typeof value === 'object') {
      if (value.label != null) return value.label
      const parts = Object.values(value).filter((v) => v != null && v !== '')
      return parts.length ? parts.join(' · ') : '—'
    }
    return String(value)
  }

  function header(key) {
    return key.replace(/_/g, ' ')
  }
</script>

<div class="table-wrap">
  <table class="record-table">
    <thead>
      <tr>
        {#each columns as col}<th>{header(col)}</th>{/each}
        {#if hasRaw}<th class="raw-col">raw</th>{/if}
      </tr>
    </thead>
    <tbody>
      {#each rows as row}
        <tr>
          {#each columns as col}
            <td class:muted={row?.[col] == null || row?.[col] === ''}>{fmt(row?.[col])}</td>
          {/each}
          {#if hasRaw}
            <td class="raw-col">
              {#if row?.raw_text}
                <details>
                  <summary>raw</summary>
                  <pre class="raw">{row.raw_text}</pre>
                </details>
              {:else}
                <span class="muted">—</span>
              {/if}
            </td>
          {/if}
        </tr>
      {/each}
    </tbody>
  </table>
</div>
