import { useEffect, useRef } from 'react'
import { useAuth } from '../context/AuthContext'
import { apiFetch } from '../lib/api'

// CallLoop's own support widget — unrelated to the Intercom OAuth
// integration (IN-1..IN-8) that pulls customers' Intercom data INTO
// CallLoop for scoring. This is the opposite direction: embedding
// Intercom's chat widget in CallLoop's own UI as CallLoop's support
// channel, using CallLoop's own Intercom workspace.
//
// Hand-rolled loader, not the @intercom/messenger-js-sdk npm package:
// that package (0.0.20, self-labeled "Beta") inserts its script via
// `document.getElementsByTagName('script')[0].parentNode.insertBefore(...)`,
// which silently throws in this app's bundle — confirmed via a captured
// HAR showing the identity-hash fetch firing repeatedly (this component
// retrying after a failed boot) and zero requests ever reaching
// widget.intercom.io. This loader uses `document.head.appendChild`
// instead, which has no such dependency on an existing <script> tag.

const APP_ID = String(import.meta.env.VITE_INTERCOM_APP_ID || '').trim()
const SCRIPT_ID = '_intercom_widget_loader'

type IntercomSettings = {
  app_id: string
  api_base?: string
  user_id?: string
  email?: string
  name?: string
  intercom_user_jwt?: string
}

// Intercom migrated their security scheme after this widget was first
// built: the old raw-HMAC "user_hash" ("Identity Verification") still
// works, but Intercom's own dashboard only marks the Messenger as
// "securely installed" under the newer "Messenger Security with JWTs" —
// confirmed by testing (a correctly-computed user_hash still showed
// "Insecurely installed"). api_base is required for the JWT scheme per
// Intercom's own installation sample.
const API_BASE = 'https://api-iam.intercom.io'

declare global {
  interface Window {
    Intercom?: ((...args: unknown[]) => void) & { q?: unknown[]; c?: (args: unknown) => void }
    intercomSettings?: IntercomSettings
  }
}

function loadWidgetScript(): void {
  if (document.getElementById(SCRIPT_ID)) return
  const script = document.createElement('script')
  script.id = SCRIPT_ID
  script.type = 'text/javascript'
  script.async = true
  script.src = `https://widget.intercom.io/widget/${APP_ID}`
  document.head.appendChild(script)
}

function bootIntercom(settings: IntercomSettings): void {
  if (typeof window.Intercom === 'function') {
    window.Intercom('reattach_activator')
    window.Intercom('update', settings)
    return
  }
  // Queue placeholder — the real widget script (once loaded) drains this
  // queue and replaces window.Intercom with its own implementation. Same
  // mechanism Intercom's own classic snippet has used for years.
  const queue: unknown[] = []
  const intercom = (...args: unknown[]) => {
    queue.push(args)
  }
  intercom.q = queue
  window.Intercom = intercom
  window.intercomSettings = settings
  loadWidgetScript()
}

export function IntercomWidget() {
  const { session, email, firstName, lastName } = useAuth()
  const bootedForUserId = useRef<string | null>(null)

  useEffect(() => {
    if (!APP_ID) return

    const userId = session?.user?.id ?? null
    if (!userId) {
      // Signed out (or no session yet) — clear any previous user's widget
      // state so the next visitor never sees someone else's conversation.
      if (bootedForUserId.current && typeof window.Intercom === 'function') {
        window.Intercom('shutdown')
        bootedForUserId.current = null
      }
      return
    }

    if (bootedForUserId.current === userId) return

    let cancelled = false
    const name = [firstName, lastName].filter(Boolean).join(' ').trim()

    // Messenger Security JWT is signed server-side — the secret that
    // produces it must never reach the browser. If it's not configured
    // yet (INTERCOM_MESSENGER_SECRET unset), the widget still boots, just
    // unverified, same as Messenger Security being off in Intercom's
    // own settings.
    apiFetch('/api/support/widget-identity')
      .then((r) => (r.ok ? r.json() : { configured: false, intercom_user_jwt: null }))
      .catch(() => ({ configured: false, intercom_user_jwt: null }))
      .then((data: { configured?: boolean; intercom_user_jwt?: string | null }) => {
        if (cancelled) return
        bootIntercom({
          app_id: APP_ID,
          api_base: API_BASE,
          user_id: userId,
          email: email ?? undefined,
          name: name || undefined,
          ...(data.intercom_user_jwt ? { intercom_user_jwt: data.intercom_user_jwt } : {}),
        })
        bootedForUserId.current = userId
      })

    return () => {
      cancelled = true
    }
  }, [session?.user?.id, email, firstName, lastName])

  return null
}
