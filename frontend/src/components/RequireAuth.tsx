import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

export function RequireAuth() {
  const { configured, loading, session } = useAuth()
  const location = useLocation()

  if (!configured) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  if (loading) {
    return (
      <div className="auth-page">
        <p className="auth-lead">Loading session…</p>
      </div>
    )
  }
  if (!session) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  return <Outlet />
}
