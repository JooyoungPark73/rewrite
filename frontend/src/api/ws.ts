// useServerEvents(handler): subscribe to the backend's WebSocket event stream.
//
// Connects to ws://<current host>/ws/events (Vite proxies it to the backend in
// development) and calls `handler` with every parsed JSON event. If the
// socket drops, for example because the backend restarted under
// `uvicorn --reload`, it reconnects with exponential backoff: 0.5 s, 1 s,
// 2 s, ... capped at 10 s, and back to 0.5 s once a connection succeeds.
//
// Returns the connection status so the header can show a small indicator.
import { useEffect, useRef, useState } from 'react'
import type { ServerEvent } from './client'

export type SocketStatus = 'connecting' | 'open' | 'closed'

const MIN_DELAY_MS = 500
const MAX_DELAY_MS = 10_000

function eventsUrl(): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/ws/events`
}

/** Parse a raw message; returns null for anything that is not `{type: string, ...}`. */
function parseEvent(data: unknown): ServerEvent | null {
  if (typeof data !== 'string') return null
  try {
    const value: unknown = JSON.parse(data)
    if (value && typeof value === 'object' && typeof (value as { type?: unknown }).type === 'string') {
      return value as ServerEvent
    }
  } catch {
    // Ignore malformed frames rather than tearing down the connection.
  }
  return null
}

export function useServerEvents(handler: (event: ServerEvent) => void): SocketStatus {
  const [status, setStatus] = useState<SocketStatus>('connecting')

  // Keep the latest handler in a ref so a new handler function on each render
  // does not force a reconnect. The socket always calls handlerRef.current.
  const handlerRef = useRef(handler)
  useEffect(() => {
    handlerRef.current = handler
  }, [handler])

  useEffect(() => {
    let socket: WebSocket | null = null
    let retryTimer: ReturnType<typeof setTimeout> | undefined
    let delay = MIN_DELAY_MS
    // Set by the cleanup function so a closing socket does not schedule a
    // reconnect after the component has unmounted (or after React StrictMode's
    // development-only unmount/remount).
    let disposed = false

    const connect = () => {
      if (disposed) return
      setStatus('connecting')
      socket = new WebSocket(eventsUrl())

      socket.onopen = () => {
        delay = MIN_DELAY_MS
        setStatus('open')
      }
      socket.onmessage = (msg) => {
        const event = parseEvent(msg.data)
        if (event) handlerRef.current(event)
      }
      // onerror is always followed by onclose, so reconnecting happens there.
      socket.onclose = () => {
        socket = null
        if (disposed) return
        setStatus('closed')
        retryTimer = setTimeout(connect, delay)
        delay = Math.min(delay * 2, MAX_DELAY_MS)
      }
    }

    connect()

    return () => {
      disposed = true
      clearTimeout(retryTimer)
      const s = socket
      if (!s) return
      s.onmessage = null
      // Closing a socket that is still connecting makes the browser log a
      // warning (this happens on every load in development, because React
      // StrictMode mounts, unmounts and remounts components). Close it as
      // soon as it opens instead.
      if (s.readyState === WebSocket.CONNECTING) s.onopen = () => s.close()
      else s.close()
    }
  }, [])

  return status
}
