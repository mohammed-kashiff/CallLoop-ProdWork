import { Navigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { CcDenied, CommandCenterPage } from '../components/cc/CommandCenterPage'
import { ccHint } from '../components/cc/classes'
import { ADMIN_ORIGIN, isAdminHost } from '../lib/adminHost'
import { ActivityLogTable } from './ActivityLog'

export function AdminActivityLog() {
  const { isPlatformAdmin } = useAuth()

  if (!isPlatformAdmin) {
    if (isAdminHost()) return <CcDenied title="Security log" />
    return <Navigate to="/" replace />
  }

  if (!isAdminHost()) {
    if (typeof window !== 'undefined') {
      window.location.href = `${ADMIN_ORIGIN}/admin-activity-log`
    }
    return null
  }

  return (
    <CommandCenterPage title="Security log" crumb="Command Center">
      <p className={`${ccHint} mb-6`}>
        Every real state-changing action across every org — who did it, when, what changed.
      </p>
      <ActivityLogTable endpoint="/api/admin/audit-log" showOrg variant="cc" filterable />
    </CommandCenterPage>
  )
}
