function formatZapis(text) {
  if (!text) return '';

  // === COMMAREC BRAND COLORS (guidelines 2026) ===
  const NAVY   = '#173767';   // primary navy
  const CYAN   = '#00AFF0';   // primary cyan
  const MUTED  = '#4A6080';
  const TEXT   = '#0E213E';
  const BG_ROW = '#F6F9FC';

  // Strip HTML artefacts AI sometimes generates
  text = text.replace(/font-weight:[^;]+;color:#[0-9a-fA-F]+">/g, '');
  text = text.replace(/<[^>]+>/g, '');
  text = text.replace(/style="[^"]*"/g, '');

  function md(s) {
    s = s.replace(/font-weight:[^;]*;?color:[^;]*/g, '').replace(/style="[^"]*"/g, '');
    return s
      .replace(/\*\*([^*]+)\*\*/g, `<strong style="font-weight:700;color:${NAVY}">$1</strong>`)
      .replace(/"([^"]{3,80})"/g, `<em style="color:${MUTED}">"$1"</em>`);
  }

  function scoreBadge(pct) {
    // Brand palette from guidelines color book
    let bg;
    if      (pct >= 70) bg = '#34C759';   // green
    else if (pct >= 55) bg = '#00AFF0';   // cyan (brand)
    else if (pct >= 40) bg = '#FF8D00';   // orange
    else                bg = '#FF383C';   // red (FF383C from guidelines)
    return `<span style="background:${bg};color:#fff;font-size:11px;font-weight:700;`
         + `padding:3px 11px;border-radius:20px;display:inline-block;letter-spacing:0.03em">${pct}%</span>`;
  }

  const lines = text.split('\n');
  let html = '', inUl = false, inTable = false;

  const closeUl    = () => { if (inUl)    { html += '</ul>'; inUl = false; } };
  const closeTable = () => { if (inTable) { html += '</tbody></table></div>'; inTable = false; } };
  const closeAll   = () => { closeUl(); closeTable(); };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();

    if (!line) { closeAll(); html += '<div style="height:8px"></div>'; continue; }

    // Horizontal rule
    if (/^-{3,}$/.test(line)) {
      closeAll();
      html += `<hr style="border:none;border-top:1px solid #dde4ed;margin:16px 0">`;
      continue;
    }

    // === TABLE ===
    if (line.includes('|') && line.split('|').filter(c => c.trim()).length >= 2) {
      const cells = line.split('|').map(c => c.trim()).filter(c => c);
      if (cells.every(c => /^[-:]+$/.test(c))) continue; // skip separator

      if (!inTable) {
        closeUl();
        html += `<div style="overflow-x:auto;margin:12px 0">`
              + `<table style="width:100%;border-collapse:collapse;font-size:13px">`;
        html += `<thead><tr>`;
        cells.forEach(c => {
          html += `<th style="text-align:left;font-size:10px;font-weight:700;color:${MUTED};`
                + `text-transform:uppercase;letter-spacing:0.08em;padding:9px 14px;`
                + `border-bottom:2px solid ${CYAN};background:#f0f5fb">${md(c)}</th>`;
        });
        html += `</tr></thead><tbody>`;
        inTable = true;
        continue;
      }
      html += `<tr>`;
      cells.forEach(c => {
        const m = c.trim().match(/^(\d+)\s*%$/);
        if (m) {
          html += `<td style="padding:9px 14px;border-bottom:1px solid #e8edf4;vertical-align:middle">${scoreBadge(parseInt(m[1]))}</td>`;
        } else {
          html += `<td style="padding:9px 14px;border-bottom:1px solid #e8edf4;font-size:13px;color:${TEXT};vertical-align:top">${md(c)}</td>`;
        }
      });
      html += `</tr>`;
      continue;
    }
    closeTable();

    // === SECTION HEADING — ALL CAPS ===
    const isAllCaps = line.length > 3
      && line === line.toUpperCase()
      && /[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]/.test(line)
      && !/^[•\-–*0-9]/.test(line)
      && !line.includes('|');

    if (isAllCaps) {
      closeAll();
      // Large, readable, navy — Druk Condensed per guidelines
      html += `<div style="`
            + `font-family:'DrukCondensed','Impact',sans-serif;`
            + `font-size:22px;`          // big enough to read
            + `font-weight:900;`
            + `color:${NAVY};`           // navy #173767
            + `text-transform:uppercase;`
            + `letter-spacing:0.04em;`
            + `line-height:1.15;`
            + `border-bottom:2.5px solid ${CYAN};`
            + `padding-bottom:6px;`
            + `margin:22px 0 10px`
            + `">${line}</div>`;
      continue;
    }

    // === SUB-HEADING — ends with colon ===
    if (line.endsWith(':') && !/^[•\-–*]/.test(line) && line.length < 80 && !line.includes('|')) {
      closeUl();
      const clean = line.replace(/\*\*/g, '').slice(0, -1);
      html += `<div style="`
            + `font-family:'Montserrat',sans-serif;`
            + `font-size:12px;`
            + `font-weight:700;`
            + `color:${NAVY};`
            + `text-transform:uppercase;`
            + `letter-spacing:0.09em;`
            + `margin:14px 0 5px`
            + `">${clean}</div>`;
      continue;
    }

    // === BULLET ===
    if (/^[•\-–]\s/.test(line) || (line.startsWith('* ') && !line.startsWith('**'))) {
      const t = line.replace(/^[•\-–*]\s+/, '');
      if (!inUl) { html += `<ul style="list-style:none;padding:0;margin:4px 0">`; inUl = true; }
      html += `<li style="font-size:13px;line-height:1.75;padding:3px 0 3px 20px;position:relative;color:${TEXT}">`
            + `<span style="position:absolute;left:0;color:${CYAN};font-weight:900;font-size:15px;line-height:1.5">•</span>`
            + `${md(t)}</li>`;
      continue;
    }

    // === NUMBERED LIST ===
    if (/^\d+\.\s/.test(line)) {
      const num = line.match(/^(\d+)\./)[1];
      const t   = line.replace(/^\d+\.\s*/, '');
      if (!inUl) { html += `<ul style="list-style:none;padding:0;margin:4px 0">`; inUl = true; }
      html += `<li style="font-size:13px;line-height:1.75;padding:3px 0 3px 22px;position:relative;color:${TEXT}">`
            + `<span style="position:absolute;left:0;color:${CYAN};font-weight:700;font-size:11px">${num}.</span>`
            + `${md(t)}</li>`;
      continue;
    }

    closeUl();
    html += `<p style="font-size:13px;line-height:1.75;margin:3px 0;color:${TEXT}">${md(line)}</p>`;
  }
  closeAll();
  return html;
}
