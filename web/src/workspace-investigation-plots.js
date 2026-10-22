import { select, scaleLinear, axisBottom, axisLeft, line } from 'd3';
import { plotSegments, supportIntervals, investigationPresentation, finitePlotDomain } from './workspace-investigation-data.js';

const dimensions = { width: 600, height: 210, left: 55, right: 16, top: 18, bottom: 32 };
const series = (label, className, value, angular = false) => ({ label, className, value, angular });
const pathLine = line().x(point => point[0]).y(point => point[1]);

export function createInvestigationPlots(container, data, onSelect) {
  const presentation = investigationPresentation(data);
  const estimate = frame => frame[presentation.poseKey];
  const reference = frame => presentation.showReference ? frame.reference : null;
  let interval = [0, data.frames.length - 1];
  let cursor = 0;
  let markers = [];
  let charts = [];
  let destroyed = false;
  let resizeFrame;
  const root = select(container);
  root.html('<div class="investigation-projections"></div><div class="investigation-signals"></div>');

  function chart(parent, title, note, className = '') {
    const node = parent.append('section').attr('class', `investigation-chart surface ${className}`);
    node.append('h3').text(title);
    if (note) node.append('p').attr('class', 'muted').text(note);
    const svg = node.append('svg').attr('role', 'img').attr('aria-label', title);
    svg.append('title').text(title);
    return { node, svg };
  }
  function axes(svg, x, y, width, height, left = dimensions.left) {
    svg.append('g').attr('class', 'plot-axis').attr('transform', `translate(0,${height - dimensions.bottom})`).call(axisBottom(x).ticks(Math.max(3, Math.floor(width / 100))));
    svg.append('g').attr('class', 'plot-axis').attr('transform', `translate(${left},0)`).call(axisLeft(y).ticks(4));
  }
  function addLegend(node, entries) {
    const legend = node.append('div').attr('class', 'plot-legend');
    for (const item of entries) legend.append('span').attr('class', item.className).text(item.label);
  }
  function buildProjection(parent, rows, a, b, axisNames) {
    const { node, svg } = chart(parent, `${axisNames[0]}–${axisNames[1]} trajectory`, `${presentation.worldLabel} · m · equal axis scale`);
    const width = Math.max(260, node.node().clientWidth - 28);
    const height = 260;
    svg.attr('viewBox', `0 0 ${width} ${height}`);
    const positions = rows.flatMap(frame => [estimate(frame)?.position_m, reference(frame)?.position_m].filter(Boolean));
    const xe = finitePlotDomain(positions.map(p => p[a]));
    const ye = finitePlotDomain(positions.map(p => p[b]));
    if (!xe || !ye) { node.append('p').text('Values exceed the supported display range. Exact observations remain in the retained evidence.'); return; }
    const plotWidth = width - dimensions.left - dimensions.right;
    const plotHeight = height - dimensions.top - dimensions.bottom;
    const unit = Math.max((xe[1] - xe[0]) / plotWidth, (ye[1] - ye[0]) / plotHeight);
    const xc = xe[0] / 2 + xe[1] / 2;
    const yc = ye[0] / 2 + ye[1] / 2;
    const x = scaleLinear().domain([xc - unit * plotWidth / 2, xc + unit * plotWidth / 2]).range([dimensions.left, width - dimensions.right]);
    const y = scaleLinear().domain([yc - unit * plotHeight / 2, yc + unit * plotHeight / 2]).range([height - dimensions.bottom, dimensions.top]);
    axes(svg, x, y, width, height);
    for (const [pose, className] of [[reference, 'series-reference'], [estimate, 'series-estimate']]) {
      const draw = line().defined(frame => Boolean(pose(frame))).x(frame => x(pose(frame).position_m[a])).y(frame => y(pose(frame).position_m[b]));
      svg.append('path').datum(rows).attr('class', `plot-line ${className}`).attr('d', draw);
      const marker = svg.append('circle').attr('r', 4).attr('class', `plot-marker ${className}`);
      markers.push(frame => marker.attr('display', pose(frame) ? null : 'none').attr('cx', pose(frame) ? x(pose(frame).position_m[a]) : 0).attr('cy', pose(frame) ? y(pose(frame).position_m[b]) : 0));
    }
    addLegend(node, [{ label: presentation.estimateLabel, className: 'series-estimate' }, ...(presentation.showReference ? [{ label: 'Reference', className: 'series-reference' }] : [])]);
  }
  function timeChart(parent, title, note, rows, definitions, domain) {
    const { node, svg } = chart(parent, title, note);
    const width = Math.max(280, node.node().clientWidth - 28);
    const height = dimensions.height;
    svg.attr('viewBox', `0 0 ${width} ${height}`);
    const x = scaleLinear().domain(domain[0] === domain[1] ? [domain[0] - .025, domain[1] + .025] : domain).range([dimensions.left, width - dimensions.right]);
    const allValues = rows.flatMap(row => definitions.map(def => def.value(row)));
    const range = finitePlotDomain(allValues);
    if (!range) { node.append('p').text('Values exceed the supported display range. Exact observations remain in the retained evidence.'); return; }
    const y = scaleLinear().domain(range).range([height - dimensions.bottom, dimensions.top]);
    axes(svg, x, y, width, height);
    for (const def of definitions) {
      const segments = plotSegments(rows, def.value, { angular: def.angular, maxPoints: 1200 });
      for (const points of segments) {
        svg.append('path').attr('class', `plot-line ${def.className}`).attr('d', pathLine(points.map(([time, value]) => [x(time), y(value)])));
        if (points.length === 1) svg.append('circle').attr('class', `plot-marker ${def.className}`).attr('r', 2).attr('cx', x(points[0][0])).attr('cy', y(points[0][1]));
      }
    }
    const marker = svg.append('line').attr('class', 'plot-cursor').attr('y1', dimensions.top).attr('y2', height - dimensions.bottom);
    markers.push(frame => marker.attr('x1', x(frame.time_s)).attr('x2', x(frame.time_s)));
    svg.on('click', event => {
      const rect = svg.node().getBoundingClientRect();
      onSelect(x.invert((event.clientX - rect.left) * width / rect.width));
    });
    addLegend(node, definitions);
    charts.push(svg);
  }
  function supportChart(parent, rows, domain) {
    const { node, svg } = chart(parent, 'Sample support', 'Missing output and unsupported reference are separate conditions.');
    const width = Math.max(280, node.node().clientWidth - 28);
    const height = 150;
    const left = 95;
    const x = scaleLinear().domain(domain[0] === domain[1] ? [domain[0] - .025, domain[1] + .025] : domain).range([left, width - dimensions.right]);
    svg.attr('viewBox', `0 0 ${width} ${height}`);
    for (const [i, [key, label]] of [['estimated', 'Estimate'], ['reference_supported', 'Reference'], ['evaluated', 'Evaluated']].entries()) {
      const y = 20 + i * 32;
      svg.append('text').attr('class', 'support-label').attr('x', left - 10).attr('y', y + 14).attr('text-anchor', 'end').text(label);
      for (const band of supportIntervals(rows, key)) svg.append('rect').attr('class', `support-band ${band.supported ? 'supported' : 'unsupported'}`).attr('x', x(band.start)).attr('y', y).attr('width', Math.max(1, x(band.end) - x(band.start))).attr('height', 20);
    }
    svg.append('g').attr('class', 'plot-axis').attr('transform', 'translate(0,120)').call(axisBottom(x).ticks(Math.max(3, Math.floor(width / 100))));
    const marker = svg.append('line').attr('class', 'plot-cursor').attr('y1', 15).attr('y2', 115);
    markers.push(frame => marker.attr('x1', x(frame.time_s)).attr('x2', x(frame.time_s)));
    svg.on('click', event => { const box = svg.node().getBoundingClientRect(); onSelect(x.invert((event.clientX - box.left) * width / box.width)); });
    addLegend(node, [{ label: 'Available', className: 'support-yes' }, { label: 'Missing / unsupported', className: 'support-no' }]);
  }
  function draw() {
    if (destroyed) return;
    markers = [];
    charts = [];
    const projection = root.select('.investigation-projections').html('');
    const signals = root.select('.investigation-signals').html('');
    const rows = data.frames.slice(interval[0], interval[1] + 1);
    const domain = [rows[0].time_s, rows.at(-1).time_s];
    for (const [a, b, names] of [[0, 1, ['X', 'Y']], [0, 2, ['X', 'Z']], [1, 2, ['Y', 'Z']]]) buildProjection(projection, rows, a, b, names);
    if (presentation.accuracyAvailable) {
      timeChart(signals, 'Position error', 'Euclidean distance · m · evaluated samples only', rows, [series('Position error', 'series-estimate', row => row.position_error_m)], domain);
      timeChart(signals, 'Orientation error', 'Quaternion geodesic angle · deg · evaluated samples only', rows, [series('Orientation error', 'series-estimate', row => row.orientation_error_deg)], domain);
    }
    supportChart(signals, rows, domain);
    for (const [axis, label] of ['Roll', 'Pitch', 'Yaw'].entries()) timeChart(signals, `${label} attitude`, `Body → ${presentation.worldLabel.toLowerCase()} · deg · principal Euler angles`, rows, [series(presentation.estimateLabel, 'series-estimate', row => estimate(row)?.rpy_deg[axis], true), ...(presentation.showReference ? [series('Reference', 'series-reference', row => reference(row)?.rpy_deg[axis], true)] : [])], domain);
    const imu = data.imu.samples.filter(sample => sample.time_s >= domain[0] && sample.time_s <= domain[1]);
    for (const [key, label, unit] of [['gyro_rad_s', 'angular velocity', 'rad/s'], ['accel_m_s2', 'specific force', 'm/s²']]) timeChart(signals, `${presentation.imuLabel} ${label === 'specific force' && !presentation.privateInput ? 'acceleration' : label}`, `${presentation.imuFrame} · ${unit} · ${imu.length.toLocaleString('en')} input samples in interval`, imu, ['X', 'Y', 'Z'].map((label, axis) => series(label, `series-axis-${axis}`, row => row[key][axis])), domain);
    setCursor(cursor);
  }
  function setCursor(index) {
    cursor = index;
    for (const update of markers) update(data.frames[cursor]);
  }
  const observer = new ResizeObserver(() => {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(draw);
  });
  observer.observe(container);
  draw();
  return {
    setCursor,
    setInterval(value) { interval = value; draw(); },
    destroy() { destroyed = true; observer.disconnect(); cancelAnimationFrame(resizeFrame); for (const svg of charts) svg.on('click', null); root.html(''); markers = []; },
  };
}
