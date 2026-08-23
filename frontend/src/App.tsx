import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AppLayout } from './components/AppLayout'
import { AuditProvider } from './context/AuditContext'
import { ColorModeProvider } from './context/ColorMode'
import { PyaiStatusProvider } from './context/PyaiStatus'
import { UsageEnvProvider } from './context/UsageEnv'
import { AgentsPulse } from './pages/AgentsPulse'
import { ChurnRisk } from './pages/ChurnRisk'
import { Feedbacks } from './pages/Feedbacks'
import { FlaggedForReview } from './pages/FlaggedForReview'
import { Home } from './pages/Home'
import { Integrations } from './pages/Integrations'
import { Neighbourhood } from './pages/Neighbourhood'
import { Pyai } from './pages/Pyai'
import { Training } from './pages/Training'
import './App.css'
import './live.css'

function App() {
  return (
    <ColorModeProvider>
      <UsageEnvProvider>
        <PyaiStatusProvider>
          <BrowserRouter>
            <AuditProvider>
              <Routes>
                <Route element={<AppLayout />}>
                  <Route index element={<Home />} />
                  <Route path="neighbourhood" element={<Neighbourhood />} />
                  <Route path="agents-pulse" element={<AgentsPulse />} />
                  <Route path="agents-pulse/flagged" element={<FlaggedForReview />} />
                  <Route path="feedbacks" element={<Feedbacks />} />
                  <Route path="churn-risk" element={<ChurnRisk />} />
                  <Route path="integrations" element={<Integrations />} />
                  <Route path="training" element={<Training />} />
                  <Route path="pyai" element={<Pyai />} />
                  <Route path="*" element={<Navigate to="/" replace />} />
                </Route>
              </Routes>
            </AuditProvider>
          </BrowserRouter>
        </PyaiStatusProvider>
      </UsageEnvProvider>
    </ColorModeProvider>
  )
}

export default App
