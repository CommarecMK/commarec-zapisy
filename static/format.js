function formatZapis(text) {
  if (!text) return '';

  const NAVY = '#173767';
  const CYAN = '#00AFF0';
  const MUTED = '#4A6080';

  function md(s) {
    return s
      .replace(/\*\*([^*]+)\*\*/g, '<strong style="font-weight:700;color:' + NAVY + ';">$1</strong>')
      .replace(/\*([^*]+)\*/g, '<em>$1</em>')
      .replace(/"([^"]+)"/g, '<em style="color:' + MUTED + ';">"$1"</em>');
  }

  const lines = text.split('\n');
  let html = '';
  let inUl = false, inTable = false;

  function closeUl() { if (inUl) { html += '</ul>'; inUl = false; } }
  function closeTable() { if (inTable) { html += '</tbody></table></div>'; inTable = false; } }
  function closeAll() { closeUl(); closeTable(); }

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const line = raw.trim();
    if (!line) { closeAll(); html += '<div style="height:5px"></div>'; continue; }

    if (line.includes('|') && line.split('|').length >= 3) {
      const cells = line.split('|').map(c => c.trim()).filter(c => c);
      if (cells.every(c => /^[-:]+$/.test(c))) continue;
      if (!inTable) {
        closeUl();
        html += '<div style="overflow-x:auto;margin:8px 0"><table style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr>';
        cells.forEach(c => { html += `<th style="text-align:left;font-size:10px;font-weight:700;color:${MUTED};text-transform:uppercase;letter-spacing:0.08em;padding:6px 10px;border-bottom:2px solid ${CYAN}">${md(c)}</th>`; });
        html += '</tr></thead><tbody>';
        inTable = true;
      } else {
        html += '<tr>';
        cells.forEach(c => {
          const isScore = /^\d+\s*%$/.test(c.trim());
          if (isScore) {
            const pct = parseInt(c);
            const col = pct >= 65 ? '#0A7A5A' : pct >= 45 ? '#BA7517' : '#C0392B';
            html += `<td style="padding:6px 10px;border-bottom:1px solid #e4eaf2"><span style="background:${col};color:white;font-size:11px;font-weight:700;padding:2px 8px;border-radius:20px">${c.trim()}</span></td>`;
          } else {
            html += `<td style="padding:6px 10px;border-bottom:1px solid #e4eaf2;font-size:13px;vertical-align:top">${md(c)}</td>`;
          }
        });
        html += '</tr>';
      }
      continue;
    }
    closeTable();

    const h1 = line.match(/^#\s+(.+)/);
    const h2 = line.match(/^##\s+(.+)/);
    const h3 = line.match(/^###\s+(.+)/);
    if (h1) {
      closeAll();
      html += `<div style="font-family:'DrukCondensed','Impact',sans-serif;font-size:20px;font-weight:900;color:${NAVY};text-transform:uppercase;letter-spacing:0.05em;border-bottom:2px solid ${CYAN};padding-bottom:5px;margin:18px 0 8px">${h1[1]}</div>`;
      continue;
    }
    if (h2) {
      closeAll();
      html += `<div style="font-family:'DrukCondensed','Impact',sans-serif;font-size:16px;font-weight:900;color:${NAVY};text-transform:uppercase;letter-spacing:0.06em;border-bottom:1.5px solid ${CYAN};padding-bottom:4px;margin:14px 0 6px">${h2[1]}</div>`;
      continue;
    }
    if (h3) {
      closeUl();
      html += `<div style="font-family:'Montserrat',sans-serif;font-size:11px;font-weight:700;color:${MUTED};text-transform:uppercase;letter-spacing:0.1em;margin:10px 0 4px">${h3[1]}</div>`;
      continue;
    }

    const isAllCaps = line === line.toUpperCase() && line.length > 3
      && !/[0-9*#\-•]/.test(line[0]) && /[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]/.test(line);
    if (isAllCaps) {
      closeAll();
      html += `<div style="font-family:'DrukCondensed','Impact',sans-serif;font-size:15px;font-weight:900;color:${NAVY};text-transform:uppercase;letter-spacing:0.08em;border-bottom:2px solid ${CYAN};padding-bottom:4px;margin:16px 0 7px">${line}</div>`;
      continue;
    }

    if (/^\*\*[^*]+\*\*:?\s*$/.test(line)) {
      const clean = line.replace(/\*\*/g, '').replace(/:$/, '');
      closeUl();
      html += `<div style="font-family:'Montserrat',sans-serif;font-size:12px;font-weight:700;color:${NAVY};text-transform:uppercase;letter-spacing:0.08em;margin:10px 0 4px">${clean}</div>`;
      continue;
    }

    if (line.endsWith(':') && !/^[•\-–*]/.test(line) && line.length < 80 && !line.includes('|')) {
      const clean = line.replace(/\*\*/g, '').slice(0, -1);
      closeUl();
      html += `<div style="font-family:'Montserrat',sans-serif;font-size:12px;font-weight:700;color:${NAVY};text-transform:uppercase;letter-spacing:0.08em;margin:10px 0 4px">${clean}</div>`;
      continue;
    }

    if (/^[•\-–]\s/.test(line) || (line.startsWith('* ') && !line.startsWith('**'))) {
      const t = line.replace(/^[•\-–*]\s+/, '');
      if (!inUl) { html += '<ul style="list-style:none;padding:0;margin:2px 0">'; inUl = true; }
      html += `<li style="font-size:13px;line-height:1.7;padding:2px 0 2px 16px;position:relative;color:#1a2540"><span style="position:absolute;left:0;color:${CYAN};font-weight:700">•</span>${md(t)}</li>`;
      continue;
    }

    if (/^\d+\.\s/.test(line)) {
      const t = line.replace(/^\d+\.\s*/, '');
      if (!inUl) { html += '<ul style="list-style:none;padding:0;margin:2px 0">'; inUl = true; }
      html += `<li style="font-size:13px;line-height:1.7;padding:2px 0 2px 16px;position:relative;color:#1a2540"><span style="position:absolute;left:0;color:${CYAN};font-weight:700">›</span>${md(t)}</li>`;
      continue;
    }

    closeUl();
    html += `<p style="font-size:13px;line-height:1.7;margin:3px 0;color:#1a2540">${md(line)}</p>`;
  }
  closeAll();
  return html;
}
