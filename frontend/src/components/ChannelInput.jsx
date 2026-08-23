import { useState } from 'react'
import axios from 'axios'

export default function ChannelInput({ onTaskStart }) {
  const [value, setValue] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const handleSubmit = async (e) => {
    e.preventDefault()
    const trimmed = value.trim()
    if (!trimmed) return

    setLoading(true)
    setError(null)

    try {
      const res = await axios.post('/api/parse', { channel: trimmed })
      onTaskStart(res.data.task_id)
    } catch (err) {
      setError(err.response?.data?.detail || err.message || 'Ошибка при запуске')
    } finally {
      setLoading(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="input-form">
      <input
        type="text"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="@username, https://t.me/channel или ID канала"
        disabled={loading}
        className="channel-input"
      />
      <button type="submit" disabled={loading || !value.trim()} className="submit-btn">
        {loading ? 'Запуск...' : 'Скачать канал'}
      </button>
      {error && <p className="error-msg">{error}</p>}
    </form>
  )
}
