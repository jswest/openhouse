<script>
  // The right-hand reference column: the parsed record (what the parser produced)
  // and the PDF's extracted text (what it had to work from), so the reviewer can
  // judge precision and recall against the embedded PDF beside it.
  let { filing } = $props()

  const recordJson = $derived(
    JSON.stringify({ filing: filing.filing, body: filing.body }, null, 2),
  )
</script>

<div class="record">
  {#if filing.stale}
    <div class="banner">
      This filing changed since it was last reviewed (snapshot mismatch) —
      re-check it and save again.
    </div>
  {/if}

  <h2>Parsed record</h2>
  <pre>{recordJson}</pre>

  <h2>Extracted text</h2>
  {#if filing.raw_text}
    <pre class="rawtext">{filing.raw_text}</pre>
  {:else}
    <p class="empty">
      No extractable text — this is a scanned image. Trades the PDF shows but the
      record omits are an OCR gap, not a parser bug.
    </p>
  {/if}
</div>
