import { Navigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { ADMIN_ORIGIN, isAdminHost } from '../lib/adminHost'
import { ActivityLogTable } from './ActivityLog'

// AC-63/AC-69: every org's activity, in one place — Command Center,
// platform admin only. Same gate/host pattern as PlatformAdmins.tsx.

export function AdminActivityLog() {
  const { isPlatformAdmin } = useAuth()

  if (!isPlatformAdmin) {
    if (isAdminHost()) {
      return (
        <>
          <header className="page-bar">
            <div>
              <p className="crumb">Command Center</p>
              <h1>Activity Log</h1>
            </div>
          </header>
          <p className="admin-provision-hint">This console is limited to platform admins.</p>
        </>
      )
    }
    return <Navigate to="/" replace />
  }

  if (!isAdminHost()) {
    if (typeof window !== 'undefined') {
      window.location.href = `${ADMIN_ORIGIN}/admin-activity-log`
    }
    return null
  }

  return (
    <>
      <header className="page-bar">
        <div>
          <p className="crumb">Command Center</p>
          <h1>Activity Log</h1>
        </div>
      </header>
      <p className="scaffold-banner">
        Every real state-changing action across every org — who did it, when, what changed.
      </p>
      <ActivityLogTable endpoint="/api/admin/audit-log" showOrg />
    </>
  )
}
