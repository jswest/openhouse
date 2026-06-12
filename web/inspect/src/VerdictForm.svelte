<script>
  import { postVerdict } from './api.js'

  // The 2×2 verdict: entries (precise/recalled) + metadata (accurate/complete),
  // with optional magnitude counts on the two entry dimensions. The server stamps
  // the snapshot and enforces the count>0 ⟺ false invariant; this form just keeps
  // a valid shape — a count is sent only when its dimension is marked imperfect.
  let { docId, existing, onsaved } = $props()

  let precise = $state(true)
  let recalled = $state(true)
  let metaAccurate = $state(true)
  let metaComplete = $state(true)
  let nIncorrect = $state('')
  let nMissing = $state('')
  let note = $state('')
  let error = $state(null)
  let saving = $state(false)

  // Re-seed the form whenever the selected filing changes (docId), pre-filling
  // from an existing verdict so re-review starts from the prior answer.
  $effect(() => {
    docId // track the filing switch
    precise = existing ? existing.is_fully_precise : true
    recalled = existing ? existing.is_fully_recalled : true
    metaAccurate = existing ? existing.is_metadata_accurate : true
    metaComplete = existing ? existing.is_metadata_fully_complete : true
    nIncorrect = existing?.n_incorrect_entries != null ? String(existing.n_incorrect_entries) : ''
    nMissing = existing?.n_missing_entries != null ? String(existing.n_missing_entries) : ''
    note = existing?.note ?? ''
    error = null
  })

  // A count rides along only when its dimension is imperfect and a positive
  // number was typed; otherwise null ("fine" or "wrong, didn't tally").
  function countOrNull(good, raw) {
    if (good) return null
    const n = parseInt(raw, 10)
    return Number.isFinite(n) && n > 0 ? n : null
  }

  async function save() {
    saving = true
    error = null
    try {
      await postVerdict(docId, {
        is_fully_precise: precise,
        is_fully_recalled: recalled,
        n_incorrect_entries: countOrNull(precise, nIncorrect),
        n_missing_entries: countOrNull(recalled, nMissing),
        is_metadata_accurate: metaAccurate,
        is_metadata_fully_complete: metaComplete,
        note: note.trim() || null,
      })
      onsaved?.()
    } catch (e) {
      error = e.message
    } finally {
      saving = false
    }
  }
</script>

<div class="verdict">
  <p class="legend">Check what's TRUE of the parse. Uncheck → optionally tally how many.</p>

  <div class="dim">
    <label class="check">
      <input type="checkbox" bind:checked={precise} />
      Entries fully <strong>precise</strong> — nothing hallucinated or wrong
    </label>
    {#if !precise}
      <div class="count">
        # incorrect <input type="number" min="1" bind:value={nIncorrect} placeholder="?" />
      </div>
    {/if}
  </div>

  <div class="dim">
    <label class="check">
      <input type="checkbox" bind:checked={recalled} />
      Entries fully <strong>recalled</strong> — nothing in the PDF missed
    </label>
    {#if !recalled}
      <div class="count">
        # missing <input type="number" min="1" bind:value={nMissing} placeholder="?" />
      </div>
    {/if}
  </div>

  <div class="dim">
    <label class="check">
      <input type="checkbox" bind:checked={metaAccurate} />
      Metadata <strong>accurate</strong> — filer / district / date / type right
    </label>
  </div>

  <div class="dim">
    <label class="check">
      <input type="checkbox" bind:checked={metaComplete} />
      Metadata <strong>complete</strong> — nothing wrongly missing
    </label>
  </div>

  <textarea bind:value={note} placeholder="note — ground truth for scanned cases, or a correction hint"></textarea>

  {#if error}<p class="banner" style="margin-top:0.5rem">{error}</p>{/if}

  <div class="actions">
    <button class="save" onclick={save} disabled={saving}>
      {existing ? 'Update verdict' : 'Save verdict'}
    </button>
  </div>
</div>
