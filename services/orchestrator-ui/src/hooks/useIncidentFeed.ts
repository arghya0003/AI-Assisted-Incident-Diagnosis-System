import { useEffect, useRef, useState } from 'react'
import { wsUrl } from '../api'
import type { FeedMessage } from '../types'

export type ConnectionStatus = 'connecting' | 'open' | 'closed'

/** Subscribes to the orchestrator's live incident feed (app/ws.py) and calls `onMessage` for
 * every lifecycle event. Reconnects with a short fixed backoff on drop, since a human sitting
 * on this console needs the feed back quickly, not a slow exponential backoff tuned for a
 * background worker. */
export function useIncidentFeed(onMessage: (msg: FeedMessage) => void): ConnectionStatus {
  const [status, setStatus] = useState<ConnectionStatus>('connecting')
  const onMessageRef = useRef(onMessage)

  useEffect(() => {
    onMessageRef.current = onMessage
  }, [onMessage])

  useEffect(() => {
    let socket: WebSocket | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    let cancelled = false

    const connect = () => {
      setStatus('connecting')
      socket = new WebSocket(wsUrl())
      socket.onopen = () => setStatus('open')
      socket.onmessage = (event) => {
        try {
          onMessageRef.current(JSON.parse(event.data) as FeedMessage)
        } catch {
          // Malformed frame: ignore rather than crash the console over one bad message.
        }
      }
      socket.onclose = () => {
        setStatus('closed')
        if (!cancelled) reconnectTimer = setTimeout(connect, 3000)
      }
      socket.onerror = () => socket?.close()
    }

    connect()
    return () => {
      cancelled = true
      if (reconnectTimer) clearTimeout(reconnectTimer)
      socket?.close()
    }
  }, [])

  return status
}
