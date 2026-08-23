export default function Stats({ stats, onReset }) {
  if (!stats) return null

  const rows = [
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
      <button className="reset-btn" onClick={onReset}>Загрузить ещё один канал</button>
    </div>
  )
}
