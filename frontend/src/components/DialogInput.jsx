import { useEffect, useState } from 'react'
import axios from 'axios'

export default function DialogInput({ onTaskStart }) {
  const [value, setValue] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [dialogs, setDialogs] = useState([])
  const [dialogsError, setDialogsError] = useState(null)
  const [loadedDialogs, setLoadedDialogs] = useState(false)
  const [asr, setAsr] = useState(null)

  useEffect(() => {
    let cancelled = false
    axios.get('/api/asr-model')
      .then((res) => { if (!cancelled) setAsr(res.data) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    let cancelled = false
    axios.get('/api/dialogs')
      .then((res) => { if (!cancelled) setDialogs(res.data) })
      .catch((err) => {
        if (!cancelled) setDialogsError(err.response?.data?.detail || 'Не удалось получить список чатов')
      })
      .finally(() => { if (!cancelled) setLoadedDialogs(true) })
    return () => { cancelled = true }
  }, [])

  const start = async (target) => {
    const trimmed = (target ?? value).trim()
    if (!trimmed) return

    setLoading(true)
    setError(null)

    try {
      const res = await axios.post('/api/parse-dm', { dialog: trimmed })
      onTaskStart(res.data.task_id)
    } catch (err) {
      setError(err.response?.data?.detail || err.message || 'Ошибка при запуске')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="dm-input">
      <form onSubmit={(e) => { e.preventDefault(); start() }} className="input-form">
        <input
          type="text"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="@username, ID пользователя или me (Избранное)"
          disabled={loading}
          className="channel-input"
        />
        <button type="submit" disabled={loading || !value.trim()} className="submit-btn">
          {loading ? 'Запуск...' : 'Скачать переписку'}
        </button>
        {error && <p className="error-msg">{error}</p>}
      </form>

      <p className="dm-hint">
        Голосовые сообщения и кружки распознаются локальной моделью и попадают
        в общую хронологию чата вместе с текстом.
      </p>

      {asr && (
        <span className={`asr-badge ${asr.gpu ? 'asr-badge--gpu' : 'asr-badge--cpu'}`}>
          {asr.gpu ? '⚡' : '🖥'} {asr.device} · модель {asr.model.split('/').pop()}
        </span>
      )}

      {dialogsError && <p className="error-msg">{dialogsError}</p>}

      {loadedDialogs && !dialogsError && dialogs.length > 0 && (
        <div className="dm-picker">
          <h3>Ваши личные чаты</h3>
          <div className="dm-picker-list">
            {dialogs.map((d) => (
              <button
                key={d.id}
                className="dm-picker-item"
                disabled={loading}
                onClick={() => start(d.username ? `@${d.username}` : d.id)}
              >
                <span className="dm-picker-name">
                  {d.name}
                  {d.parsed && <span className="dm-parsed-badge">уже скачан</span>}
                </span>
                <span className="dm-picker-meta">
                  {d.username ? `@${d.username}` : d.id}
                  {d.last_message_at && ` · ${d.last_message_at}`}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
