import { useCallback, useEffect, useRef, useState } from 'react'
import ChannelInput from './components/ChannelInput'
import DialogInput from './components/DialogInput'
import DownloadedChannels from './components/DownloadedChannels'
import History from './components/History'
import ParsedDialogs from './components/ParsedDialogs'
import Progress from './components/Progress'
import Stats from './components/Stats'
import './App.css'

// view: 'main' | 'channels' | 'dialogs'
// source: 'channel' | 'dm'  — what the main screen is set up to download
export default function App() {
  const [view, setView] = useState('main')
  const [source, setSource] = useState('channel')
  const [taskId, setTaskId] = useState(null)
  const [taskSource, setTaskSource] = useState('channel')
  const [progress, setProgress] = useState(null)
  const [finalStats, setFinalStats] = useState(null)
  const [historyKey, setHistoryKey] = useState(0)
  const wsRef = useRef(null)

  const handleTaskStart = useCallback((id, kind) => {
    setTaskId(id)
    setTaskSource(kind)
    setProgress(null)
    setFinalStats(null)

    const ws = new WebSocket(`/ws/progress/${id}`)
    wsRef.current = ws

    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data)
      if (msg.error) {
        setFinalStats({ error: msg.error })
        ws.close()
        setHistoryKey((k) => k + 1)
        return
      }
      setProgress(msg)
      if (msg.done) {
        setFinalStats(msg.stats)
        ws.close()
        setHistoryKey((k) => k + 1)
      }
    }

    ws.onerror = () => {
      setFinalStats({ error: 'Потеряно соединение с сервером' })
    }
  }, [])

  useEffect(() => {
    return () => wsRef.current?.close()
  }, [])

  const handleReset = () => {
    setTaskId(null)
    setProgress(null)
    setFinalStats(null)
  }

  if (view === 'channels') {
    return (
      <div className="app">
        <header className="app-header">
          <h1>Telegram Channel Parser</h1>
          <p className="app-subtitle">Полная выгрузка канала для AI-аналитики</p>
        </header>
        <main className="app-main">
          <DownloadedChannels onBack={() => setView('main')} />
        </main>
      </div>
    )
  }

  if (view === 'dialogs') {
    return (
      <div className="app">
        <header className="app-header">
          <h1>Telegram Channel Parser</h1>
          <p className="app-subtitle">Полная выгрузка канала для AI-аналитики</p>
        </header>
        <main className="app-main">
          <ParsedDialogs onBack={() => setView('main')} />
        </main>
      </div>
    )
  }

  const showInput = !taskId || finalStats
  const showProgress = taskId && !finalStats && progress
  const showStats = finalStats && !finalStats.error
  const showError = finalStats?.error

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-header-top">
          <div>
            <h1>Telegram Channel Parser</h1>
            <p className="app-subtitle">Полная выгрузка канала для AI-аналитики</p>
          </div>
          <div className="nav-group">
            <button className="nav-btn" onClick={() => setView('channels')}>
              Скачанные каналы
            </button>
            <button className="nav-btn" onClick={() => setView('dialogs')}>
              Скачанные переписки
            </button>
          </div>
        </div>
      </header>

      <main className="app-main">
        {showInput && (
          <div className="source-tabs">
            <button
              className={`source-tab ${source === 'channel' ? 'source-tab--active' : ''}`}
              onClick={() => setSource('channel')}
            >
              Канал
            </button>
            <button
              className={`source-tab ${source === 'dm' ? 'source-tab--active' : ''}`}
              onClick={() => setSource('dm')}
            >
              Личные сообщения
            </button>
          </div>
        )}

        {showInput && source === 'channel' && (
          <ChannelInput onTaskStart={(id) => handleTaskStart(id, 'channel')} />
        )}

        {showInput && source === 'dm' && (
          <DialogInput onTaskStart={(id) => handleTaskStart(id, 'dm')} />
        )}

        {showProgress && <Progress data={progress} mode={taskSource} />}

        {showStats && <Stats stats={finalStats} onReset={handleReset} mode={taskSource} />}

        {showError && (
          <div className="error-block">
            <p>Ошибка: {finalStats.error}</p>
            <button className="reset-btn" onClick={handleReset}>Попробовать снова</button>
          </div>
        )}

        <History refreshKey={historyKey} />
      </main>
    </div>
  )
}
