// Image-derived captions (TA-5 / IN-11) are stored as normal ticket_messages
// rows. The scorecard must never present that synthetic text as a verbatim
// quote of what someone typed. has_image on the cited turn is the signal —
// no extra column, and captions stay in the scoring prompt.

type TicketEvidenceProps = {
  text: string | null
  isImage: boolean
  assetUrl?: string
  verified?: boolean
}

export function TicketEvidence({ text, isImage, assetUrl, verified }: TicketEvidenceProps) {
  if (isImage) {
    return (
      <figure className="evidence is-image">
        {assetUrl ? <img src={assetUrl} alt="Attached screenshot" /> : null}
        <figcaption>
          <span className="evidence-image-label">[image]</span>
          {text ? ` ${text}` : null}
        </figcaption>
      </figure>
    )
  }
  if (!text) return null
  return (
    <blockquote className="evidence">
      <p>&ldquo;{text}&rdquo;</p>
      {verified === false ? (
        <span className="evidence-unverified">Quote not verified verbatim</span>
      ) : null}
    </blockquote>
  )
}
