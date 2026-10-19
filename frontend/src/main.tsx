// Entry point: mounts <App /> into #root and pulls in the global stylesheet
// (Tailwind + DaisyUI + CodeMirror tweaks).
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.tsx'
import './index.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
