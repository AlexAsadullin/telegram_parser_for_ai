export default function Stats({ stats, onReset, mode = 'channel' }) {
  if (!stats) return null

  const isDm = mode === 'dm'

  const clock = (seconds) => {
    const m = Math.floor((seconds || 0) / 60)
    const s = Math.floor((seconds || 0) % 60)
    return `${m}:${String(s).padStart(2, '0')}`
  }

  const rows = isDm
    ? [
        ['Собеседник', stats.channel_title || stats.channel_username || stats.channel_id],
        ['Username', stats.channel_username ? `@${stats.channel_username}` : '—'],
        ['Всего сообщений', stats.total_posts],
        ['Сохранено', stats.saved_posts],
        ['Пропущено', stats.skipped_posts],
        ['Голосовые', stats.voice_count],
        ['Кружки', stats.videos_count],
        ['Распознано речью', `${stats.transcribed_count} из ${(stats.voice_count || 0) + (stats.videos_count || 0)}`],
        ['Длительность речи', `${clock(stats.transcribed_seconds)} из ${clock(stats.voice_seconds)}`],
      ]
    : [
        ['Канал', stats.channel_title || stats.channel_username || stats.channel_id],
        ['Username', stats.channel_username ? `@${stats.channel_username}` : '—'],
        ['Всего постов', stats.total_posts],
        ['Сохранено', stats.saved_posts],
        ['Пропущено', stats.skipped_posts],
        ['Фото', stats.photos_count],
        ['Видео / кружочки', stats.videos_count],
        ['Голосовые', stats.voice_count],
        ['Файлы', stats.files_count],
        ['Ссылки', stats.links_count],
      ]

  return (
    <div className="stats-block">
      <h2>Загрузка завершена</h2>
      <table className="stats-table">
        <tbody>
          {rows.map(([label, value]) => (
            <tr key={label}>
              <td className="stats-label">{label}</td>
              <td className="stats-value">{value}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <button className="reset-btn" onClick={onReset}>
        {isDm ? 'Скачать ещё одну переписку' : 'Загрузить ещё один канал'}
      </button>
    </div>
  )
}
