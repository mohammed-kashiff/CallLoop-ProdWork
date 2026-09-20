import { BrowserRouter, Navigate, Outlet, Route, Routes } from 'react-router-dom'
import type { ReactNode } from 'react'
import { AppLayout } from './components/AppLayout'
import { RequireAuth } from './components/RequireAuth'
import { AuditProvider } from './context/AuditContext'
import { AuthProvider } from './context/AuthContext'
import { ColorModeProvider } from './context/ColorMode'
import { PyaiStatusProvider } from './context/PyaiStatus'
import { UsageEnvProvider } from './context/UsageEnv'
import { Admin } from './pages/Admin'
import { ActivityLog } from './pages/ActivityLog'
import { AdminActivityLog } from './pages/AdminActivityLog'
import { AgentsPulse } from './pages/AgentsPulse'
import { AuditDetail } from './pages/AuditDetail'
import { Audits } from './pages/Audits'
import { CallLogs } from './pages/CallLogs'
import { CallTrail } from './pages/CallTrail'
import { TicketLogs } from './pages/TicketLogs'
import { TicketTrail } from './pages/TicketTrail'
import { ChurnRisk } from './pages/ChurnRisk'
import { Feedbacks } from './pages/Feedbacks'
import { FlaggedForReview } from './pages/FlaggedForReview'
import { Home } from './pages/Home'
import { Integrations } from './pages/Integrations'
import { KpiTargets } from './pages/KpiTargets'
import { Login } from './pages/Login'
import { Neighbourhood } from './pages/Neighbourhood'
import { PlatformAdmins } from './pages/PlatformAdmins'
import { Privacy } from './pages/Privacy'
import { Profile } from './pages/Profile'
import { ResetPassword } from './pages/ResetPassword'
import { RubricBuilder } from './pages/RubricBuilder'
import { RubricView } from './pages/RubricView'
import { Pyai } from './pages/Pyai'
import { MyTicketContributions } from './pages/MyTicketContributions'
import { TicketAudit } from './pages/TicketAudit'
import { TeamPerformance } from './pages/TeamPerformance'
import { TicketRubricBuilder } from './pages/TicketRubricBuilder'
import { Training } from './pages/Training'
import { appHomePath, isAdminHost } from './lib/adminHost'
import './App.css'
import './live.css'
import './cc.css'

function AuthedShell() {
  if (isAdminHost()) {
    return (
      <AuditProvider>
        <Outlet />
      </AuditProvider>
    )
  }
  return (
    <PyaiStatusProvider>
      <AuditProvider>
        <Outlet />
      </AuditProvider>
    </PyaiStatusProvider>
  )
}

function CustomerPage({ children }: { children: ReactNode }) {
  if (isAdminHost()) return <Navigate to="/admin" replace />
  return <>{children}</>
}

function App() {
  const adminHost = isAdminHost()
  const home = appHomePath()
  return (
    <ColorModeProvider>
      <AuthProvider>
        <UsageEnvProvider>
          <BrowserRouter>
            <Routes>
              <Route path="login" element={<Login />} />
              <Route path="reset-password" element={<ResetPassword />} />
              <Route path="privacy" element={<Privacy />} />
              <Route element={<RequireAuth />}>
                <Route element={<AuthedShell />}>
                  <Route element={<AppLayout />}>
                    <Route index element={adminHost ? <Admin /> : <Home />} />
                    <Route path="neighbourhood" element={<CustomerPage><Neighbourhood /></CustomerPage>} />
                    <Route path="agents-pulse" element={<CustomerPage><AgentsPulse /></CustomerPage>} />
                    <Route path="agents-pulse/flagged" element={<CustomerPage><FlaggedForReview /></CustomerPage>} />
                    <Route path="team-performance" element={<CustomerPage><TeamPerformance /></CustomerPage>} />
                    <Route path="audits" element={<CustomerPage><Audits /></CustomerPage>} />
                    <Route path="audits/:callId" element={<CustomerPage><AuditDetail /></CustomerPage>} />
                    <Route path="rubric-builder" element={<CustomerPage><RubricBuilder /></CustomerPage>} />
                    <Route path="rubric-view" element={<CustomerPage><RubricView /></CustomerPage>} />
                    <Route path="feedbacks" element={<CustomerPage><Feedbacks /></CustomerPage>} />
                    <Route path="churn-risk" element={<CustomerPage><ChurnRisk /></CustomerPage>} />
                    <Route path="integrations" element={<CustomerPage><Integrations /></CustomerPage>} />
                    <Route path="training" element={<CustomerPage><Training /></CustomerPage>} />
                    <Route path="admin" element={<Admin />} />
                    <Route path="call-logs" element={<CallLogs />} />
                    <Route path="call-logs/:callId/trail" element={<CallTrail />} />
                    <Route path="ticket-logs" element={<TicketLogs />} />
                    <Route path="ticket-logs/:ticketId/trail" element={<TicketTrail />} />
                    <Route path="platform-admins" element={<PlatformAdmins />} />
                    <Route path="admin-activity-log" element={<AdminActivityLog />} />
                    <Route path="activity-log" element={<CustomerPage><ActivityLog /></CustomerPage>} />
                    <Route path="ticket-audit" element={<CustomerPage><TicketAudit /></CustomerPage>} />
                    <Route path="ticket-audit/:ticketId" element={<CustomerPage><TicketAudit /></CustomerPage>} />
                    <Route path="ticket-audit-mine" element={<CustomerPage><MyTicketContributions /></CustomerPage>} />
                    <Route path="ticket-rubric-builder" element={<CustomerPage><TicketRubricBuilder /></CustomerPage>} />
                    <Route path="profile" element={<CustomerPage><Profile /></CustomerPage>} />
                    <Route path="kpi-targets" element={<CustomerPage><KpiTargets /></CustomerPage>} />
                    <Route path="pyai" element={<CustomerPage><Pyai /></CustomerPage>} />
                    <Route path="*" element={<Navigate to={home} replace />} />
                  </Route>
                </Route>
              </Route>
            </Routes>
          </BrowserRouter>
        </UsageEnvProvider>
      </AuthProvider>
    </ColorModeProvider>
  )
}

export default App
