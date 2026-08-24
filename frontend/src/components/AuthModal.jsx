import { useState } from 'react'
import axios from 'axios'

// step: 'phone' | 'code' | 'password'
export default function AuthModal({ onAuthorized }) {
  const [step, setStep] = useState('phone')
  const [phone, setPhone] = useState('')
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const call = async (url, payload, onSuccess) => {
    setBusy(true)
    setError(null)
    try {
      const res = await axios.post(url, payload)
      onSuccess(res.data)
    } catch (err) {
      setError(err.response?.data?.detail || err.message || 'Что-то пошло не так')
    } finally {
      setBusy(false)
    }
  }

  const submitPhone = (e) => {
    e.preventDefault()
    call('/api/auth/send-code', { phone }, (data) => {
      if (data.already_authorized) onAuthorized(data.user)
      else setStep('code')
    })
  }

  const submitCode = (e) => {
    e.preventDefault()
    call('/api/auth/sign-in', { code }, (data) => {
      if (data.password_required) setStep('password')
      else if (data.authorized) onAuthorized(data.user)
    })
  }

  const submitPassword = (e) => {
    e.preventDefault()
    call('/api/auth/password', { password }, (data) => {
      if (data.authorized) onAuthorized(data.user)
    })
  }

  const restart = async () => {
    setError(null)
    setCode('')
    setPassword('')
    setStep('phone')
    try {
      await axios.post('/api/auth/cancel')
    } catch {
      // nothing useful to do — the user is starting over anyway
    }
  }

  return (
    <div className="auth-overlay">
      <div className="auth-modal">
        <h2>Вход в Telegram</h2>

        {step === 'phone' && (
          <form onSubmit={submitPhone}>
            <p className="auth-lead">
              Введите номер телефона аккаунта — Telegram пришлёт код подтверждения.
            </p>
            <input
              type="tel"
              className="auth-input"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              placeholder="+79991234567"
              autoFocus
              disabled={busy}
            />
            <button type="submit" className="auth-btn" disabled={busy || !phone.trim()}>
              {busy ? 'Отправляю...' : 'Получить код'}
            </button>
          </form>
        )}

        {step === 'code' && (
          <form onSubmit={submitCode}>
            <p className="auth-lead">
              Код отправлен на <strong>{phone}</strong>. Проверьте приложение Telegram
              или SMS.
            </p>
            <input
              type="text"
              inputMode="numeric"
              className="auth-input"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="12345"
              autoFocus
              disabled={busy}
            />
            <button type="submit" className="auth-btn" disabled={busy || !code.trim()}>
              {busy ? 'Проверяю...' : 'Войти'}
            </button>
            <button type="button" className="auth-link" onClick={restart} disabled={busy}>
              Изменить номер
            </button>
          </form>
        )}

        {step === 'password' && (
          <form onSubmit={submitPassword}>
            <p className="auth-lead">
              На аккаунте включена двухфакторная защита. Введите облачный пароль Telegram.
            </p>
            <input
              type="password"
              className="auth-input"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Облачный пароль"
              autoFocus
              disabled={busy}
            />
            <button type="submit" className="auth-btn" disabled={busy || !password}>
              {busy ? 'Проверяю...' : 'Подтвердить'}
            </button>
            <button type="button" className="auth-link" onClick={restart} disabled={busy}>
              Начать заново
            </button>
          </form>
        )}

        {error && <p className="auth-error">{error}</p>}

        <p className="auth-note">
          Данные уходят только на ваш локальный бэкенд и в Telegram. Сессия
          сохраняется в <code>backend/session/</code>, повторный вход не потребуется.
        </p>
      </div>
    </div>
  )
}
