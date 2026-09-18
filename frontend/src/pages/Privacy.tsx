import { Link } from 'react-router-dom'
import { BrandLogo } from '../components/BrandLogo'

const LAST_UPDATED = 'September 18, 2026'

export function Privacy() {
  return (
    <div className="legal-page">
      <header className="legal-page-header">
        <Link to="/" aria-label="Go to home">
          <BrandLogo size="sm" surface="light" animate={false} />
        </Link>
        <p className="legal-page-updated">Last updated {LAST_UPDATED}</p>
      </header>

      <h1>Privacy Policy</h1>
      <p className="panel-lede">
        This policy explains what information CallLoop collects, why, and how it is handled
        when you or your organization use CallLoop&apos;s call and ticket quality-scoring
        service, including the integrations that connect CallLoop to JustCall and Intercom.
      </p>

      <section>
        <h2>Who this applies to</h2>
        <p>
          CallLoop is used by support and sales teams (&quot;Customers&quot;) to review and score
          calls and support tickets. This policy covers data CallLoop processes on behalf of a
          Customer&apos;s organization, and data about the individual people (&quot;Users&quot;)
          who log in to CallLoop on that organization&apos;s behalf. If you are a customer of one
          of our Customers and your call or conversation was scored inside CallLoop, this policy
          also covers that.
        </p>
      </section>

      <section>
        <h2>What we collect</h2>
        <ul>
          <li>
            <strong>Account and organization data</strong> — name, email address, and role for
            each User; the organization&apos;s name and workspace settings.
          </li>
          <li>
            <strong>Call and ticket content</strong> — when an organization connects JustCall or
            Intercom, CallLoop pulls the call recording and transcript, or the conversation/ticket
            text, for items that integration surfaces (completed calls; closed conversations and
            tickets). Uploaded call recordings or PDFs work the same way when a Customer uploads
            them directly instead.
          </li>
          <li>
            <strong>Scoring output</strong> — the transcript, AI-generated quality scores, and
            coaching notes CallLoop produces from the content above.
          </li>
          <li>
            <strong>Usage and log data</strong> — pages visited inside CallLoop, API request
            metadata (endpoint, timestamp, status), and error logs. We never intentionally place
            credentials, tokens, or full API keys in logs, and actively redact anything that looks
            like one.
          </li>
        </ul>
      </section>

      <section>
        <h2>How we use it</h2>
        <p>
          To run the actual product: pull calls/conversations/tickets from a connected
          integration, transcribe and diarize call audio, generate AI quality scores and coaching
          notes against an organization&apos;s own rubric, and show that back to the organization
          inside CallLoop. We also use usage and log data to operate, secure, and debug the
          service, and to detect and fix real errors quickly.
        </p>
        <p>
          We do not sell personal information, and we do not use a Customer&apos;s call or ticket
          content to train our own general-purpose models.
        </p>
      </section>

      <section>
        <h2>Who we share it with</h2>
        <p>
          CallLoop uses the following service providers to run the product. Each only receives
          the data it needs to perform its specific function, under its own data-processing
          terms:
        </p>
        <ul>
          <li><strong>JustCall</strong> and <strong>Intercom</strong> — the source of call/conversation/ticket data, only for organizations that connect them, and only with the access that organization grants during connection.</li>
          <li><strong>Anthropic (Claude)</strong> and <strong>PyAI</strong> — process call and ticket transcripts to generate transcription, diarization, and quality scores.</li>
          <li><strong>Supabase</strong> — hosts our database and handles authentication.</li>
          <li><strong>Render</strong> — hosts the CallLoop application.</li>
          <li><strong>Better Stack</strong> and <strong>Sentry</strong> — operational logging, monitoring, and error tracking.</li>
        </ul>
        <p>
          We disclose data beyond these providers only if required by law, or with an
          organization&apos;s consent.
        </p>
      </section>

      <section>
        <h2>Data retention and deletion</h2>
        <p>
          Call, conversation, and ticket data is retained for as long as an organization&apos;s
          CallLoop account is active, so scores and coaching history remain available. An
          organization&apos;s owner or manager can remove a JustCall or Intercom connection at any
          time from CallLoop&apos;s Integrations page, which stops any further data from being
          pulled; previously ingested items are not automatically deleted by disconnecting. To
          request deletion of specific data, or of an entire organization&apos;s data, contact us
          at the address below.
        </p>
      </section>

      <section>
        <h2>Security</h2>
        <p>
          Integration credentials (API keys, OAuth tokens) are stored encrypted and are never
          displayed again once saved. Access to one organization&apos;s data is isolated from
          every other organization&apos;s at the database level. We restrict who inside CallLoop
          can access customer data to what is needed to operate and support the product.
        </p>
      </section>

      <section>
        <h2>Your choices</h2>
        <p>
          Users can review what CallLoop has scored inside the product itself. Organization
          owners and managers control which integrations are connected and can disconnect them at
          any time. If you are an end customer of one of our Customers and want to know whether
          your conversation was processed by CallLoop, or want it removed, contact that
          organization directly, or reach us at the address below and we will coordinate with
          them.
        </p>
      </section>

      <section>
        <h2>Children&apos;s privacy</h2>
        <p>CallLoop is a business tool and is not directed to, and does not knowingly collect information from, children.</p>
      </section>

      <section>
        <h2>Changes to this policy</h2>
        <p>
          If we make a material change to how we handle data, we will update the date at the top
          of this page and, where appropriate, notify organization owners directly.
        </p>
      </section>

      <section>
        <h2>Contact us</h2>
        <p>
          Questions about this policy, or a request about your data, can be sent to{' '}
          <a href="mailto:privacy@call-loop.com">privacy@call-loop.com</a>.
        </p>
      </section>
    </div>
  )
}
