function formatZapis(text) {
  if (!text) return '';

  const NAVY = '#173767';
  const CYAN = '#00AFF0';
  const MUTED = '#4A6080';
  const TEXT = '#1a2540';

  // ---- Strip any HTML/CSS artefacts AI might generate ----
  text = text.replace(/font-weight:[^;]+;color:#[0-9a-fA-F]+;">?/g, '');
  text = text.replace(/<[^>]+>/g, '');
  text = text.replace(/style="[^"]*"/g, '');

  function md(s) {
    s = s.replace(/font-weight:[^;]*;?color:[^;]*;?/g, '');
    s = s.replace(/style="[^"]*"/g, '');
    return s
      .replace(/\*\*([^*]+)\*\*/g, `<strong style="font-weight:700;color:${NAVY}">$1</strong>`)
      .replace(/"([^"]{3,80})"/g, `<em style="color:${MUTED}">"$1"</em>`);
  }

  const lines = text.split('\n');
  let html = '';
  let inUl = false, inTable = false, tableHasHead = false;

  function closeUl()    { if (inUl)    { html += '</ul>'; inUl = false; } }
  function closeTable() { if (inTable) { html += '</tbody></table></div>'; inTable = false; tableHasHead = false; } }
  function closeAll()   { closeUl(); closeTable(); }

  for (let i = 0; i < lines.length; i++) {
    const raw  = lines[i];
    const line = raw.trim();

    if (!line) {
      closeAll();
      html += '<div style="height:6px"></div>';
      continue;
    }

    // Horizontal rule
    if (/^-{3,}$/.test(line)) {
      closeAll();
      html += '<hr style="border:none;border-top:1px solid #dde4ed;margin:14px 0;">';
      continue;
    }

    // ---- TABLE (pipe-separated) ----
    if (line.includes('|') && line.split('|').filter(c=>c.trim()).length >= 2) {
      const cells = line.split('|').map(c => c.trim()).filter(c => c);
      // skip separator row
      if (cells.every(c => /^[-:]+$/.test(c))) continue;

      if (!inTable) {
        closeUl();
        html += '<div style="overflow-x:auto;margin:10px 0"><table style="width:100%;border-collapse:collapse;font-size:13px">';
        html += '<thead><tr>';
        cells.forEach(c => {
          html += `<th style="text-align:left;font-size:10px;font-weight:700;color:${MUTED};text-transform:uppercase;letter-spacing:0.08em;padding:8px 12px;border-bottom:2px solid ${CYAN};background:#f6f9fc">${md(c)}</th>`;
        });
        html += '</tr></thead><tbody>';
        inTable = true;
        continue;
      }

      html += '<tr>';
      cells.forEach(c => {
        const pct = parseInt(c);
        if (/^\d+\s*%$/.test(c.trim())) {
          // Brand-color score badge
          let bg;
          if (pct >= 70) bg = '#34C759';
          else if (pct >= 55) bg = '#00AFF0';
          else if (pct >= 40) bg = '#FF8D00';
          else bg = '#FF383C';
          html += `<td style="padding:8px 12px;border-bottom:1px solid #e8edf4;vertical-align:middle">` +
                  `<span style="background:${bg};color:#fff;font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px;display:inline-block">${c.trim()}</span></td>`;
        } else {
          html += `<td style="padding:8px 12px;border-bottom:1px solid #e8edf4;font-size:13px;color:${TEXT};vertical-align:top">${md(c)}</td>`;
        }
      });
      html += '</tr>';
      continue;
    }
    closeTable();

    // ---- HEADINGS ----
    // All-caps line = section heading (Druk Condensed, navy, cyan underline)
    const isAllCaps = line.length > 3
      && line === line.toUpperCase()
      && /[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]/.test(line)
      && !/^[•\-–*0-9]/.test(line)
      && !line.includes('|');

    if (isAllCaps) {
      closeAll();
      html += `<div style="font-family:'DrukCondensed','Impact',sans-serif;` +
              `font-size:15px;font-weight:900;color:${NAVY};` +
              `text-transform:uppercase;letter-spacing:0.07em;` +
              `border-bottom:2px solid ${CYAN};padding-bottom:5px;margin:20px 0 8px;` +
              `line-height:1.2">${line}</div>`;
      continue;
    }

    // Sub-heading: short line ending in colon, not a bullet
    if (line.endsWith(':') && !/^[•\-–*]/.test(line) && line.length < 80 && !line.includes('|')) {
      closeUl();
      const clean = line.replace(/\*\*/g, '').slice(0, -1);
      html += `<div style="font-family:'Montserrat',sans-serif;font-size:11px;font-weight:700;` +
              `color:${NAVY};text-transform:uppercase;letter-spacing:0.09em;margin:12px 0 4px">${clean}:</div>`;
      continue;
    }

    // ---- BULLETS ----
    if (/^[•\-–]\s/.test(line) || (line.startsWith('* ') && !line.startsWith('**'))) {
      const t = line.replace(/^[•\-–*]\s+/, '');
      if (!inUl) { html += '<ul style="list-style:none;padding:0;margin:3px 0">'; inUl = true; }
      html += `<li style="font-size:13px;line-height:1.75;padding:2px 0 2px 18px;position:relative;color:${TEXT}">` +
              `<span style="position:absolute;left:0;color:${CYAN};font-weight:700">•</span>${md(t)}</li>`;
      continue;
    }

    // Numbered list
    if (/^\d+\.\s/.test(line)) {
      const num = line.match(/^(\d+)\./)[1];
      const t   = line.replace(/^\d+\.\s*/, '');
      if (!inUl) { html += '<ul style="list-style:none;padding:0;margin:3px 0">'; inUl = true; }
      html += `<li style="font-size:13px;line-height:1.75;padding:2px 0 2px 22px;position:relative;color:${TEXT}">` +
              `<span style="position:absolute;left:0;color:${CYAN};font-weight:700;font-size:11px">${num}.</span>${md(t)}</li>`;
      continue;
    }

    closeUl();
    html += `<p style="font-size:13px;line-height:1.75;margin:3px 0;color:${TEXT}">${md(line)}</p>`;
  }
  closeAll();
  return html;
}
