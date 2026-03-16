function formatZapis(text) {
  const lines = text.split('\n');
  let html = '';
  let inUl = false, inTable = false;

  function closeUl() { if(inUl) { html += '</ul>'; inUl = false; } }
  function closeTable() { if(inTable) { html += '</tbody></table></div>'; inTable = false; } }
  function closeAll() { closeUl(); closeTable(); }

  for (let i = 0; i < lines.length; i++) {
    let line = lines[i].trim();
    if (!line) { closeAll(); html += '<div style="height:6px"></div>'; continue; }

    // Table row
    if (line.includes('|') && line.split('|').length >= 3) {
      const cells = line.split('|').map(c => c.trim()).filter(c => c);
      if (cells.every(c => /^[-:]+$/.test(c))) continue;
      if (!inTable) {
        closeUl();
        html += '<div style="overflow-x:auto;margin:8px 0;"><table style="width:100%;border-collapse:collapse;font-size:13px;"><thead><tr>';
        cells.forEach(c => { html += `<th style="text-align:left;font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:0.07em;padding:6px 10px;background:var(--bg);border-bottom:1px solid var(--border);">${c}</th>`; });
        html += '</tr></thead><tbody>';
        inTable = true;
      } else {
        html += '<tr>';
        cells.forEach(c => {
          const isScore = /^\d+\s*%$/.test(c);
          if (isScore) {
            const pct = parseInt(c);
            const col = pct >= 65 ? '#0A7A5A' : pct >= 45 ? '#BA7517' : '#C0392B';
            html += `<td style="padding:6px 10px;border-bottom:1px solid var(--border);"><span style="background:${col};color:white;font-size:11px;font-weight:700;padding:2px 8px;border-radius:20px;">${c}</span></td>`;
          } else {
            html += `<td style="padding:6px 10px;border-bottom:1px solid var(--border);font-size:13px;vertical-align:top;">${c}</td>`;
          }
        });
        html += '</tr>';
      }
      continue;
    }
    closeTable();

    // Section heading — all caps
    const isAllCaps = line === line.toUpperCase() && line.length > 3 && !line.startsWith('•') && !/\d{2}/.test(line);
    if (isAllCaps) {
      closeAll();
      html += `<div style="font-size:12px;font-weight:700;color:var(--navy);text-transform:uppercase;letter-spacing:0.09em;border-bottom:2px solid var(--cyan);padding-bottom:5px;margin:16px 0 8px;">${line}</div>`;
      continue;
    }

    // Subsection (ends with colon, short line)
    if (line.endsWith(':') && !line.startsWith('•') && !line.startsWith('-') && line.length < 80) {
      closeUl();
      html += `<div style="font-size:12px;font-weight:700;color:var(--navy);text-transform:uppercase;letter-spacing:0.05em;margin:10px 0 4px;">${line.slice(0,-1)}</div>`;
      continue;
    }

    // Bullet
    if (line.startsWith('•') || line.startsWith('-') || line.startsWith('\u2013')) {
      const t = line.replace(/^[•\-\u2013]\s*/, '');
      if (!inUl) { html += '<ul style="list-style:none;padding:0;margin:0;">'; inUl = true; }
      html += `<li style="font-size:13px;line-height:1.7;padding:2px 0 2px 16px;position:relative;"><span style="position:absolute;left:0;color:var(--cyan);font-weight:700;">•</span>${t}</li>`;
      continue;
    }

    // Numbered
    if (/^\d+\.\s/.test(line)) {
      const t = line.replace(/^\d+\.\s*/, '');
      if (!inUl) { html += '<ul style="list-style:none;padding:0;margin:0;">'; inUl = true; }
      html += `<li style="font-size:13px;line-height:1.7;padding:2px 0 2px 16px;position:relative;"><span style="position:absolute;left:0;color:var(--cyan);font-weight:700;">›</span>${t}</li>`;
      continue;
    }

    closeUl();
    html += `<p style="font-size:13px;line-height:1.7;margin-bottom:4px;">${line}</p>`;
  }
  closeAll();
  return html;
}
