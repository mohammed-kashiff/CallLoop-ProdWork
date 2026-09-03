import { BrowserRouter, Navigate, Outlet, Route, Routes } from 'react-router-dom'
import { AppLayout } from './components/AppLayout'
import { RequireAuth } from './components/RequireAuth'
import { AuditProvider } from './context/AuditContext'
import { AuthProvider } from './context/AuthContext'
import { ColorModeProvider } from './context/ColorMode'
import { PyaiStatusProvider } from './context/PyaiStatus'
import { UsageEnvProvider } from './context/UsageEnv'
import { Admin } from './pages/Admin'
import { AgentsPulse } from './pages/AgentsPulse'
import { AuditDetail } from './pages/AuditDetail'
import { Audits } from './pages/Audits'
import { CallLogs } from './pages/CallLogs'
import { ChurnRisk } from './pages/ChurnRisk'
import { Feedbacks } from './pages/Feedbacks'
import { FlaggedForReview } from './pages/FlaggedForReview'
import { Home } from './pages/Home'
import { Integrations } from './pages/Integrations'
import { Login } from './pages/Login'
import { Neighbourhood } from './pages/Neighbourhood'
import { Profile } from './pages/Profile'
import { ResetPassword } from './pages/ResetPassword'
import { Pyai } from './pages/Pyai'
import { Training } from './pages/Training'
import { appHomePath, isAdminHost } from './lib/adminHost'
import './App.css'
import './live.css'

function AuthedShell() {
  return (
    <PyaiStatusProvider>
      <AuditProvider>
        <Outlet />
      </AuditProvider>
    </PyaiStatusProvider>
  )
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
              <Route element={<RequireAuth />}>
                <Route element={<AuthedShell />}>
                  <Route element={<AppLayout />}>
                    <Route index element={adminHost ? <Admin /> : <Home />} />
                    <Route path="neighbourhood" element={<Neighbourhood />} />
                    <Route path="agents-pulse" element={<AgentsPulse />} />
                    <Route path="agents-pulse/flagged" element={<FlaggedForReview />} />
                    <Route path="audits" element={<Audits />} />
                    <Route path="audits/:callId" element={<AuditDetail />} />
                    <Route path="feedbacks" element={<Feedbacks />} />
                    <Route path="churn-risk" element={<ChurnRisk />} />
                    <Route path="integrations" element={<Integrations />} />
                    <Route path="training" element={<Training />} />
                    <Route path="admin" element={<Admin />} />
                    <Route path="call-logs" element={<CallLogs />} />
                    <Route path="profile" element={<Profile />} />
                    <Route path="pyai" element={<Pyai />} />
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
