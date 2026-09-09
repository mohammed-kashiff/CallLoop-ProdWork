import { useEffect, useRef } from 'react'
import Intercom, { shutdown as shutdownIntercom } from '@intercom/messenger-js-sdk'
import { useAuth } from '../context/AuthContext'
import { apiFetch } from '../lib/api'

// CallLoop's own support widget — unrelated to the Intercom OAuth
// integration (IN-1..IN-8) that pulls customers' Intercom data INTO
// CallLoop for scoring. This is the opposite direction: embedding
// Intercom's chat widget in CallLoop's own UI as CallLoop's support
// channel, using CallLoop's own Intercom workspace.
const APP_ID = String(import.meta.env.VITE_INTERCOM_APP_ID || '').trim()

export function IntercomWidget() {
  const { session, email, firstName, lastName } = useAuth()
  const bootedForUserId = useRef<string | null>(null)

  useEffect(() => {
    if (!APP_ID) return

    const userId = session?.user?.id ?? null
    if (!userId) {
      // Signed out (or no session yet) — clear any previous user's widget
      // state so the next visitor never sees someone else's conversation.
      if (bootedForUserId.current) {
        shutdownIntercom()
        bootedForUserId.current = null
      }
      return
    }

    if (bootedForUserId.current === userId) return

    let cancelled = false
    const name = [firstName, lastName].filter(Boolean).join(' ').trim()

    // Messenger Security hash (user_hash) is computed server-side — the
    // secret that produces it must never reach the browser. If it's not
    // configured yet (INTERCOM_MESSENGER_SECRET unset), the widget still
    // boots, just unverified, same as Messenger Security being off in
    // Intercom's own settings.
    apiFetch('/api/support/widget-identity')
      .then((r) => (r.ok ? r.json() : { configured: false, user_hash: null }))
      .catch(() => ({ configured: false, user_hash: null }))
      .then((data: { configured?: boolean; user_hash?: string | null }) => {
        if (cancelled) return
        Intercom({
          app_id: APP_ID,
          user_id: userId,
          email: email ?? undefined,
          name: name || undefined,
          ...(data.user_hash ? { user_hash: data.user_hash } : {}),
        })
        bootedForUserId.current = userId
      })

    return () => {
      cancelled = true
    }
  }, [session?.user?.id, email, firstName, lastName])

  return null
}
