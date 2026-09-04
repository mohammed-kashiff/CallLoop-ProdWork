import { useEffect, useState } from 'react'
import { useAuth } from '../context/AuthContext'
import { clearImpersonating, getImpersonating } from '../lib/supabase'

/** Shown only on the customer-facing host, only after a Command Center
 * "Log in as" tab establishes its session — never in Command Center itself
 * (see AppLayout's adminHost gate). */
export function ImpersonationBanner() {
  const { signOut } = useAuth()
  const [info, setInfo] = useState(() => getImpersonating())

  useEffect(() => {
    setInfo(getImpersonating())
  }, [])

  if (!info) return null

  return (
    <div className="impersonation-banner" role="alert">
      <span>
        Viewing as support — <strong>{info.orgName}</strong> ({info.targetEmail})
      </span>
      <button
        type="button"
        className="impersonation-banner-exit"
        onClick={() => {
          clearImpersonating()
          void signOut()
        }}
      >
        Exit
      </button>
    </div>
  )
}
