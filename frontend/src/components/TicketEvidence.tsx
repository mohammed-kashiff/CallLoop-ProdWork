// Image-derived captions (TA-5 / IN-11) are stored as normal ticket_messages
// rows. The scorecard must never present that synthetic text as a verbatim
// quote of what someone typed. has_image on the cited turn is the signal —
// no extra column, and captions stay in the scoring prompt.

export function ticketTurnDomId(seq: number, scope?: string): string {
  return scope ? `ticket-turn-${scope}-${seq}` : `ticket-turn-${seq}`
}

type TicketEvidenceProps = {
  text: string | null
  isImage: boolean
  assetUrl?: string
  verified?: boolean
  onJump?: () => void
}

export function TicketEvidence({
  text,
  isImage,
  assetUrl,
  verified,
  onJump,
}: TicketEvidenceProps) {
  const jumpable = Boolean(onJump)
  const unverified =
    verified === false ? (
      <span className="evidence-unverified">Quote not verified verbatim</span>
    ) : null

  if (isImage) {
    const body = (
      <>
        {assetUrl ? <img src={assetUrl} alt="" /> : null}
        <span className="evidence-image-caption">
          <span className="evidence-image-label">[image]</span>
          {text ? ` ${text}` : null}
        </span>
      </>
    )
    if (jumpable) {
      return (
        <button
          type="button"
          className="evidence is-image is-jump"
          onClick={onJump}
          aria-label="Show this attachment in the thread"
        >
          {body}
          {unverified}
        </button>
      )
    }
    return (
      <figure className="evidence is-image">
        {assetUrl ? <img src={assetUrl} alt="Attached screenshot" /> : null}
        <figcaption>
          <span className="evidence-image-label">[image]</span>
          {text ? ` ${text}` : null}
        </figcaption>
        {unverified}
      </figure>
    )
  }
  if (!text) return null
  if (jumpable) {
    return (
      <button
        type="button"
        className="evidence is-jump"
        onClick={onJump}
        aria-label="Show this quote in the thread"
      >
        <p>&ldquo;{text}&rdquo;</p>
        {unverified}
      </button>
    )
  }
  return (
    <blockquote className="evidence">
      <p>&ldquo;{text}&rdquo;</p>
      {unverified}
    </blockquote>
  )
}
