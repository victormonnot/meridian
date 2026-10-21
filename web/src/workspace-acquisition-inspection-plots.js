import { select, scaleLinear, axisBottom, axisLeft, line } from 'd3';
import { finite, plotDomain, plotRuns, relativeSeconds } from './workspace-acquisition-inspection-data.js';

const margins = { left: 58, right: 18, top: 18, bottom: 32 };

export function createInspectionPlots(container, view, onSelect) {
  let destroyed = false;
  let frame;
  let cursor;
  let markers = [];
  const root = select(container);
  const origin = view.primary.interval.first_ns;
  const stop = relativeSeconds(view.primary.interval.last_ns, origin);
  const domain = stop === 0 ? [-.001, .001] : [0, stop];

  function chart(title, note, height = 205) {
    const node = root.append('section').attr('class', 'inspection-chart surface');
    node.append('h3').text(title);
    node.append('p').attr('class', 'muted').text(note);
    const width = Math.max(280, node.node().clientWidth - 28);
    const svg = node.append('svg').attr('viewBox', `0 0 ${width} ${height}`)
      .attr('role', 'img').attr('aria-label', title);
    svg.append('title').text(title);
    return { node, svg, width, height };
  }

  function axes(svg, x, y, width, height) {
    svg.append('g').attr('class', 'plot-axis')
      .attr('transform', `translate(0,${height - margins.bottom})`)
      .call(axisBottom(x).ticks(Math.max(3, Math.floor(width / 100))));
    svg.append('g').attr('class', 'plot-axis').attr('transform', `translate(${margins.left},0)`)
      .call(axisLeft(y).ticks(4));
  }

  function selection(svg, x, width, height) {
    const marker = svg.append('line').attr('class', 'plot-cursor')
      .attr('y1', margins.top).attr('y2', height - margins.bottom);
    markers.push(ns => marker.attr('x1', x(relativeSeconds(ns, origin)))
      .attr('x2', x(relativeSeconds(ns, origin))));
    svg.on('click', event => {
      const box = svg.node().getBoundingClientRect();
      const time = x.invert((event.clientX - box.left) * width / box.width);
      const points = view.primary.display.points;
      if (!points.length) return;
      let nearest = points[0];
      for (const point of points) {
        if (Math.abs(relativeSeconds(point.timestamp_ns, origin) - time)
          < Math.abs(relativeSeconds(nearest.timestamp_ns, origin) - time)) nearest = point;
      }
      onSelect(nearest.timestamp_ns);
    });
  }

  function signal(title, note, window, definitions) {
    const rows = window.display.points;
    const { node, svg, width, height } = chart(title, note);
    const domainY = plotDomain(rows.flatMap(point => definitions.map(def => def.value(point))));
    if (domainY === null) {
      svg.remove();
      node.append('p').attr('class', 'inspection-boundary').text('Signal plot unavailable: the numeric range exceeds finite display coordinates. Exact selected observations and full-resolution evidence remain available.');
      return;
    }
    const x = scaleLinear().domain(domain).range([margins.left, width - margins.right]);
    const y = scaleLinear().domain(domainY)
      .range([height - margins.bottom, margins.top]);
    axes(svg, x, y, width, height);
    for (const gap of window.gaps) {
      const first = Math.max(0, relativeSeconds(gap.previous_timestamp_ns, origin));
      const last = Math.min(stop, relativeSeconds(gap.timestamp_ns, origin));
      if (first >= last) continue;
      svg.append('rect').attr('class', 'inspection-gap').attr('x', x(first)).attr('y', margins.top)
        .attr('width', Math.max(1, x(last) - x(first))).attr('height', height - margins.top - margins.bottom);
    }
    for (const [axis, definition] of definitions.entries()) {
      for (const rowsInRun of plotRuns(rows)) {
        const draw = line().defined(point => finite(definition.value(point)))
          .x(point => x(relativeSeconds(point.timestamp_ns, origin))).y(point => y(definition.value(point)));
        svg.append('path').datum(rowsInRun).attr('class', `plot-line series-axis-${axis}`).attr('d', draw);
        if (rowsInRun.length === 1 && finite(definition.value(rowsInRun[0]))) {
          svg.append('circle').attr('class', `plot-marker series-axis-${axis}`).attr('r', 2)
            .attr('cx', x(relativeSeconds(rowsInRun[0].timestamp_ns, origin))).attr('cy', y(definition.value(rowsInRun[0])));
        }
      }
    }
    const legend = node.append('div').attr('class', 'plot-legend');
    definitions.forEach((definition, axis) => legend.append('span').attr('class', `series-axis-${axis}`).text(definition.label));
    selection(svg, x, width, height);
  }

  function imu(stream, window) {
    for (const [key, title, unit] of [['gyro_rad_s', 'Angular velocity', 'rad/s'],
      ['specific_force_m_s2', 'Specific force', 'm/s²']]) {
      signal(`${stream.id} · ${title}`,
        `${stream.frame_id} · ${unit} · ${window.display.point_count} displayed / ${window.sample_count} observations`,
        window, ['X', 'Y', 'Z'].map((label, axis) => ({ label, value: point => point[key][axis] })));
    }
  }

  function histogram() {
    const bins = view.primary.timing.histogram;
    const { svg, width, height } = chart('Sample interval distribution', 'Every adjacent interval in the selection · milliseconds · no display reduction', 190);
    if (!bins.length) {
      svg.append('text').attr('x', 16).attr('y', 45).attr('class', 'support-label').text('At least two observations are required.');
      return;
    }
    const low = Number(BigInt(bins[0].lower_ns)) / 1e6;
    const high = Number(BigInt(bins.at(-1).upper_ns)) / 1e6;
    const x = scaleLinear().domain(low === high ? [Math.max(0, low - .001), high + .001] : [low, high])
      .range([margins.left, width - margins.right]);
    const y = scaleLinear().domain([0, Math.max(1, ...bins.map(bin => bin.count))])
      .range([height - margins.bottom, margins.top]);
    axes(svg, x, y, width, height);
    for (const bin of bins) {
      const a = x(Number(BigInt(bin.lower_ns)) / 1e6);
      const b = x(Number(BigInt(bin.upper_ns)) / 1e6);
      const bar = svg.append('rect').attr('class', 'inspection-histogram-bar')
        .attr('x', a).attr('y', y(bin.count)).attr('width', Math.max(2, b - a - 1))
        .attr('height', height - margins.bottom - y(bin.count));
      bar.append('title').text(`${bin.lower_ns}–${bin.upper_ns} ns: ${bin.count} intervals`);
    }
  }

  function draw() {
    if (destroyed) return;
    root.html('');
    markers = [];
    const { primary, stream, linked } = view;
    if (primary.display.status === 'available' && primary.display.points.length) {
      if (stream.kind === 'imu') imu(stream, primary);
      if (stream.kind === 'camera') {
        signal('Image intensity evidence', 'Preview-scale indicators · unitless · no exposure or blur verdict', primary, [
          { label: 'Mean brightness', value: point => point.image.indicators?.brightness_mean_0_1 },
          { label: 'Mean edge contrast', value: point => point.image.indicators?.edge_contrast_mean_0_1 },
        ]);
      }
      if (stream.kind === 'reference_pose') {
        signal('Reference observations', 'Reference support only · no estimator output or accuracy evaluation', primary,
          [{ label: 'Observation present', value: () => 1 }]);
      }
    }
    if (linked?.window.display.status === 'available' && linked.window.display.points.length) imu(linked.stream, linked.window);
    histogram();
    if (cursor) markers.forEach(update => update(cursor));
  }

  const observer = new ResizeObserver(() => {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(draw);
  });
  observer.observe(container);
  draw();
  return {
    setCursor(ns) { cursor = ns; markers.forEach(update => update(ns)); },
    destroy() {
      destroyed = true;
      observer.disconnect();
      cancelAnimationFrame(frame);
      root.selectAll('svg').on('click', null);
      root.html('');
      markers = [];
    },
  };
}
