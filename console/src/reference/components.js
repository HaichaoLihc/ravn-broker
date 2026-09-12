import React from 'react';
const __ds_scope = {};
const __ds_ns = {__errors: []};
// components/core/Icon.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
const LUCIDE_BASE = '/console/reference/';

/* Monochrome glyph. Lucide (16px grid, 2px stroke) is loaded from CDN as a CSS
   mask so the glyph inherits currentColor like a real icon font would. */
function Icon({
  name,
  size = 16,
  strokeColor,
  style,
  ...rest
}) {
  const url = `url("${LUCIDE_BASE}${name}.svg")`;
  return /*#__PURE__*/React.createElement("span", _extends({
    "aria-hidden": "true"
  }, rest, {
    style: {
      display: 'inline-block',
      flex: '0 0 auto',
      width: size,
      height: size,
      background: strokeColor || 'currentColor',
      WebkitMaskImage: url,
      maskImage: url,
      WebkitMaskSize: 'contain',
      maskSize: 'contain',
      WebkitMaskRepeat: 'no-repeat',
      maskRepeat: 'no-repeat',
      WebkitMaskPosition: 'center',
      maskPosition: 'center',
      ...style
    }
  }));
}
Object.assign(__ds_scope, { Icon });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/Icon.jsx", error: String((e && e.message) || e) }); }

// components/core/Button.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
const VARIANTS = {
  primary: {
    background: 'var(--action-primary-bg)',
    color: 'var(--action-primary-fg)',
    border: '1px solid var(--action-primary-bg)'
  },
  secondary: {
    background: 'var(--surface-overlay)',
    color: 'var(--text-primary)',
    border: '1px solid var(--border-hairline)'
  },
  destructive: {
    background: 'transparent',
    color: 'var(--status-risk)',
    border: '1px solid var(--status-risk)'
  },
  ghost: {
    background: 'transparent',
    color: 'var(--text-secondary)',
    border: '1px solid transparent'
  }
};
const HOVER = {
  primary: {
    background: 'var(--action-primary-bg-hover)',
    borderColor: 'var(--action-primary-bg-hover)'
  },
  secondary: {
    background: 'var(--surface-hover)',
    borderColor: 'var(--border-strong)'
  },
  destructive: {
    background: 'var(--status-risk)',
    color: 'var(--white)'
  },
  ghost: {
    background: 'var(--surface-hover)',
    color: 'var(--text-primary)'
  }
};

/* One primary, one secondary, one destructive. 4px radius, no arrow glyphs,
   labels are verbs describing the real action. */
function Button({
  variant = 'secondary',
  size = 'md',
  icon,
  disabled = false,
  type = 'button',
  onClick,
  style,
  children,
  ...rest
}) {
  const [hover, setHover] = React.useState(false);
  const [press, setPress] = React.useState(false);
  const base = VARIANTS[variant] || VARIANTS.secondary;
  const active = hover && !disabled ? HOVER[variant] : null;
  return /*#__PURE__*/React.createElement("button", _extends({}, rest, {
    type: type,
    disabled: disabled,
    onClick: onClick,
    onMouseEnter: () => setHover(true),
    onMouseLeave: () => {
      setHover(false);
      setPress(false);
    },
    onMouseDown: () => setPress(true),
    onMouseUp: () => setPress(false),
    style: {
      display: 'inline-flex',
      alignItems: 'center',
      gap: 'var(--icon-gap)',
      height: size === 'sm' ? 'var(--control-height-sm)' : 'var(--control-height)',
      padding: size === 'sm' ? '0 8px' : '0 var(--control-padding-x)',
      font: size === 'sm' ? 'var(--weight-medium) var(--size-caption)/1 var(--font-sans)' : 'var(--weight-medium) var(--size-body)/1 var(--font-sans)',
      borderRadius: 'var(--radius)',
      cursor: disabled ? 'not-allowed' : 'pointer',
      opacity: disabled ? 0.45 : 1,
      transition: 'background var(--motion-fast) var(--ease-out), border-color var(--motion-fast) var(--ease-out), color var(--motion-fast) var(--ease-out)',
      whiteSpace: 'nowrap',
      ...base,
      ...active,
      ...(press && !disabled && variant === 'primary' ? {
        background: 'var(--action-primary-bg-press)',
        borderColor: 'var(--action-primary-bg-press)'
      } : null),
      ...style
    }
  }), icon ? typeof icon === 'string' ? /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: icon,
    size: size === 'sm' ? 14 : 16
  }) : icon : null, children);
}
Object.assign(__ds_scope, { Button });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/Button.jsx", error: String((e && e.message) || e) }); }

// components/core/Input.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/* Labelled text input. The label sits above and stays visible — no floating
   labels, no placeholder-as-label: in a security tool a field must still say
   what it is once it has a value in it. */
function Input({
  label,
  hint,
  value,
  onChange,
  placeholder,
  mono = false,
  invalid = false,
  disabled = false,
  prefix,
  id,
  style,
  ...rest
}) {
  const [focus, setFocus] = React.useState(false);
  const inputId = id || React.useId();
  return /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      flexDirection: 'column',
      gap: 4,
      ...style
    }
  }, label ? /*#__PURE__*/React.createElement("label", {
    htmlFor: inputId,
    style: {
      font: 'var(--type-caption)',
      color: 'var(--text-muted)'
    }
  }, label) : null, /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      alignItems: 'center',
      gap: 0,
      height: 'var(--control-height)',
      padding: '0 var(--control-padding-x)',
      background: disabled ? 'var(--surface-sunken)' : 'var(--surface-overlay)',
      border: '1px solid ' + (invalid ? 'var(--status-risk)' : focus ? 'var(--corvid-violet)' : 'var(--border-hairline)'),
      borderRadius: 'var(--radius)',
      transition: 'border-color var(--motion-fast) var(--ease-out)'
    }
  }, prefix ? /*#__PURE__*/React.createElement("span", {
    style: {
      font: 'var(--type-data)',
      color: 'var(--text-muted)',
      marginRight: 2
    }
  }, prefix) : null, /*#__PURE__*/React.createElement("input", _extends({}, rest, {
    id: inputId,
    value: value,
    disabled: disabled,
    placeholder: placeholder,
    onChange: e => onChange && onChange(e.target.value),
    onFocus: () => setFocus(true),
    onBlur: () => setFocus(false),
    style: {
      flex: 1,
      minWidth: 0,
      border: 'none',
      outline: 'none',
      background: 'transparent',
      font: mono ? 'var(--type-data)' : 'var(--type-body)',
      letterSpacing: mono ? 'var(--tracking-data)' : 'var(--tracking-normal)',
      color: 'var(--text-primary)',
      padding: 0
    }
  }))), hint ? /*#__PURE__*/React.createElement("span", {
    style: {
      font: 'var(--type-caption)',
      color: invalid ? 'var(--status-risk)' : 'var(--text-muted)'
    }
  }, hint) : null);
}
Object.assign(__ds_scope, { Input });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/Input.jsx", error: String((e && e.message) || e) }); }

// components/core/SearchField.jsx
try { (() => {
/* Filter field for a view's own content. Not a global omnibox — it narrows the
   table or canvas it sits above, and says how many rows are left. */
function SearchField({
  value,
  onChange,
  placeholder = 'Filter…',
  width = 200,
  resultCount,
  style
}) {
  const [focus, setFocus] = React.useState(false);
  return /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'inline-flex',
      alignItems: 'center',
      gap: 6,
      height: 'var(--control-height)',
      width,
      padding: '0 8px 0 10px',
      background: 'var(--surface-overlay)',
      border: '1px solid ' + (focus ? 'var(--corvid-violet)' : 'var(--border-hairline)'),
      borderRadius: 'var(--radius)',
      transition: 'border-color var(--motion-fast) var(--ease-out)',
      ...style
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "search",
    size: 14,
    style: {
      color: 'var(--text-muted)',
      flex: '0 0 auto'
    }
  }), /*#__PURE__*/React.createElement("input", {
    value: value,
    placeholder: placeholder,
    onChange: e => onChange && onChange(e.target.value),
    onFocus: () => setFocus(true),
    onBlur: () => setFocus(false),
    style: {
      flex: 1,
      minWidth: 0,
      border: 'none',
      outline: 'none',
      background: 'transparent',
      font: 'var(--type-body)',
      color: 'var(--text-primary)',
      padding: 0
    }
  }), value ? /*#__PURE__*/React.createElement(React.Fragment, null, resultCount != null ? /*#__PURE__*/React.createElement("span", {
    style: {
      font: 'var(--type-caption)',
      color: 'var(--text-muted)',
      flex: '0 0 auto'
    }
  }, resultCount) : null, /*#__PURE__*/React.createElement("button", {
    type: "button",
    onClick: () => onChange && onChange(''),
    "aria-label": "Clear filter",
    style: {
      display: 'flex',
      background: 'transparent',
      border: 'none',
      color: 'var(--text-muted)',
      cursor: 'pointer',
      padding: 0,
      flex: '0 0 auto'
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "x",
    size: 13
  }))) : null);
}
Object.assign(__ds_scope, { SearchField });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/SearchField.jsx", error: String((e && e.message) || e) }); }

// components/core/Select.jsx
try { (() => {
/* Compact context picker — the org / env selectors in the top bar, and any
   in-page filter. Label sits inline before the value, sentence case. */
function Select({
  label,
  value,
  options = [],
  onChange,
  mono = false,
  width,
  style
}) {
  const [open, setOpen] = React.useState(false);
  const [hover, setHover] = React.useState(false);
  const ref = React.useRef(null);
  React.useEffect(() => {
    if (!open) return;
    const away = e => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener('mousedown', away);
    return () => document.removeEventListener('mousedown', away);
  }, [open]);
  return /*#__PURE__*/React.createElement("div", {
    ref: ref,
    style: {
      position: 'relative',
      display: 'inline-block',
      ...style
    }
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    onClick: () => setOpen(o => !o),
    onMouseEnter: () => setHover(true),
    onMouseLeave: () => setHover(false),
    style: {
      display: 'inline-flex',
      alignItems: 'center',
      gap: 'var(--icon-gap)',
      height: 'var(--control-height)',
      width,
      padding: '0 8px 0 var(--control-padding-x)',
      background: hover || open ? 'var(--surface-hover)' : 'transparent',
      border: '1px solid ' + (open ? 'var(--border-strong)' : 'var(--border-hairline)'),
      borderRadius: 'var(--radius)',
      cursor: 'pointer',
      transition: 'background var(--motion-fast) var(--ease-out)'
    }
  }, label ? /*#__PURE__*/React.createElement("span", {
    style: {
      font: 'var(--type-body)',
      color: 'var(--text-muted)'
    }
  }, label, ":") : null, /*#__PURE__*/React.createElement("span", {
    style: {
      font: mono ? 'var(--type-data)' : 'var(--type-body-medium)',
      color: 'var(--text-primary)'
    }
  }, value), /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "chevron-down",
    size: 14,
    style: {
      color: 'var(--text-muted)',
      marginLeft: 2
    }
  })), open ? /*#__PURE__*/React.createElement("div", {
    role: "listbox",
    style: {
      position: 'absolute',
      top: 'calc(100% + 4px)',
      left: 0,
      minWidth: '100%',
      zIndex: 40,
      background: 'var(--surface-overlay)',
      border: '1px solid var(--border-hairline)',
      borderRadius: 'var(--radius)',
      boxShadow: 'var(--shadow-overlay)',
      padding: '4px 0'
    }
  }, options.map(opt => /*#__PURE__*/React.createElement("div", {
    key: opt,
    role: "option",
    "aria-selected": opt === value,
    onClick: () => {
      onChange && onChange(opt);
      setOpen(false);
    },
    style: {
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      gap: 'var(--space-3)',
      height: 28,
      padding: '0 var(--control-padding-x)',
      cursor: 'pointer',
      font: mono ? 'var(--type-data)' : 'var(--type-body)',
      background: opt === value ? 'var(--surface-selected)' : 'transparent',
      color: 'var(--text-primary)',
      whiteSpace: 'nowrap'
    },
    onMouseEnter: e => {
      if (opt !== value) e.currentTarget.style.background = 'var(--surface-hover)';
    },
    onMouseLeave: e => {
      if (opt !== value) e.currentTarget.style.background = 'transparent';
    }
  }, opt, opt === value ? /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "check",
    size: 14,
    style: {
      color: 'var(--corvid-violet)'
    }
  }) : null))) : null);
}
Object.assign(__ds_scope, { Select });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/Select.jsx", error: String((e && e.message) || e) }); }

// components/core/Switch.jsx
try { (() => {
/* On/off for a policy that takes effect immediately — no Save button, so the
   control itself has to read as the setting's current state. */
function Switch({
  checked = false,
  onChange,
  disabled = false,
  label,
  hint,
  id,
  style
}) {
  const [hover, setHover] = React.useState(false);
  const switchId = id || React.useId();
  const track = /*#__PURE__*/React.createElement("button", {
    type: "button",
    role: "switch",
    id: switchId,
    "aria-checked": checked,
    disabled: disabled,
    onClick: () => onChange && onChange(!checked),
    onMouseEnter: () => setHover(true),
    onMouseLeave: () => setHover(false),
    style: {
      position: 'relative',
      flex: '0 0 auto',
      width: 30,
      height: 18,
      padding: 0,
      background: checked ? 'var(--action-primary-bg)' : 'var(--mist-strong)',
      border: '1px solid ' + (checked ? 'var(--action-primary-bg)' : hover && !disabled ? 'var(--graphite)' : 'var(--mist-strong)'),
      borderRadius: 'var(--radius-pill)',
      cursor: disabled ? 'not-allowed' : 'pointer',
      opacity: disabled ? 0.45 : 1,
      transition: 'background var(--motion-fast) var(--ease-out), border-color var(--motion-fast) var(--ease-out)'
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      position: 'absolute',
      top: 2,
      left: checked ? 14 : 2,
      width: 12,
      height: 12,
      borderRadius: '50%',
      background: 'var(--white)',
      transition: 'left var(--motion-fast) var(--ease-out)'
    }
  }));
  if (!label) return /*#__PURE__*/React.createElement("span", {
    style: style
  }, track);
  return /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      alignItems: 'flex-start',
      gap: 'var(--space-2)',
      ...style
    }
  }, track, /*#__PURE__*/React.createElement("label", {
    htmlFor: switchId,
    style: {
      display: 'flex',
      flexDirection: 'column',
      gap: 1,
      cursor: disabled ? 'not-allowed' : 'pointer',
      minWidth: 0
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      font: 'var(--type-body)'
    }
  }, label), hint ? /*#__PURE__*/React.createElement("span", {
    style: {
      font: 'var(--type-caption)',
      color: 'var(--text-muted)'
    }
  }, hint) : null));
}
Object.assign(__ds_scope, { Switch });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/Switch.jsx", error: String((e && e.message) || e) }); }

// components/data/AccessPath.jsx
try { (() => {
/* Org Root → agent → service. Three fixed steps, because that is what a
   permission IS: an org granting an agent the right to act on a service.
   (This replaced an agent→agent delegation chain, which described a runtime
   behaviour rather than a stored fact.) */
function AccessPath({
  root = 'Org Root',
  agent,
  service,
  permission,
  revoked = false,
  onSelect,
  style
}) {
  const Step = ({
    label,
    kind,
    icon,
    muted,
    clickable
  }) => /*#__PURE__*/React.createElement("span", {
    onClick: clickable && onSelect ? () => onSelect(kind, label) : undefined,
    style: {
      display: 'inline-flex',
      alignItems: 'center',
      gap: 5,
      padding: '2px 6px',
      borderRadius: 'var(--radius-hair)',
      background: kind === 'agent' ? 'var(--surface-sunken)' : 'transparent',
      color: muted ? 'var(--text-muted)' : clickable ? 'var(--text-link)' : 'var(--text-primary)',
      font: kind === 'agent' ? 'var(--type-body-medium)' : 'var(--type-body)',
      cursor: clickable && onSelect ? 'pointer' : 'default'
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: icon,
    size: 12,
    style: {
      color: 'var(--text-muted)'
    }
  }), label);
  const arrow = /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "arrow-right",
    size: 13,
    style: {
      color: 'var(--text-muted)',
      margin: '0 2px'
    }
  });
  return /*#__PURE__*/React.createElement("nav", {
    style: {
      display: 'flex',
      flexWrap: 'wrap',
      alignItems: 'center',
      gap: 'var(--space-1)',
      font: 'var(--type-body)',
      opacity: revoked ? 0.55 : 1,
      textDecoration: revoked ? 'line-through' : 'none',
      ...style
    }
  }, /*#__PURE__*/React.createElement(Step, {
    label: root,
    kind: "root",
    icon: "shield",
    muted: true
  }), arrow, /*#__PURE__*/React.createElement(Step, {
    label: agent,
    kind: "agent",
    icon: "bot",
    clickable: true
  }), permission ? /*#__PURE__*/React.createElement("span", {
    style: {
      font: 'var(--type-data)',
      color: 'var(--corvid-violet)',
      background: 'var(--violet-tint)',
      padding: '1px 5px',
      borderRadius: 'var(--radius-hair)',
      margin: '0 2px'
    }
  }, permission) : null, service ? /*#__PURE__*/React.createElement(React.Fragment, null, arrow, /*#__PURE__*/React.createElement(Step, {
    label: service,
    kind: "resource",
    icon: "database",
    clickable: true
  })) : null);
}
Object.assign(__ds_scope, { AccessPath });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/data/AccessPath.jsx", error: String((e && e.message) || e) }); }

// components/data/DataTable.jsx
try { (() => {
/* A table is a table: 36px rows, hairline mist gridlines, no zebra striping,
   no card wrapper, no shadow, no hover-lift. */
function DataTable({
  columns = [],
  rows = [],
  onRowClick,
  selectedId,
  sort,
  onSort,
  rowKey = 'id',
  emptyLabel
}) {
  const [hoverRow, setHoverRow] = React.useState(null);
  return /*#__PURE__*/React.createElement("table", {
    style: {
      font: 'var(--type-body)',
      background: 'transparent'
    }
  }, /*#__PURE__*/React.createElement("thead", null, /*#__PURE__*/React.createElement("tr", null, columns.map(col => {
    const sorted = sort && sort.key === col.key;
    return /*#__PURE__*/React.createElement("th", {
      key: col.key,
      onClick: col.sortable === false ? undefined : () => onSort && onSort(col.key),
      style: {
        height: 'var(--row-height)',
        width: col.width,
        textAlign: col.align || 'left',
        padding: '0 var(--table-cell-padding-x)',
        font: 'var(--weight-medium) var(--size-caption)/1 var(--font-sans)',
        color: sorted ? 'var(--text-primary)' : 'var(--text-muted)',
        cursor: col.sortable === false ? 'default' : 'pointer',
        userSelect: 'none',
        whiteSpace: 'nowrap'
      }
    }, /*#__PURE__*/React.createElement("span", {
      style: {
        display: 'inline-flex',
        alignItems: 'center',
        gap: 'var(--icon-gap)'
      }
    }, col.label, sorted ? /*#__PURE__*/React.createElement(__ds_scope.Icon, {
      name: sort.dir === 'asc' ? 'arrow-up' : 'arrow-down',
      size: 12
    }) : null));
  }))), /*#__PURE__*/React.createElement("tbody", null, rows.length === 0 ? /*#__PURE__*/React.createElement("tr", null, /*#__PURE__*/React.createElement("td", {
    colSpan: columns.length,
    style: {
      height: 72,
      textAlign: 'center',
      color: 'var(--text-muted)',
      font: 'var(--type-body)'
    }
  }, emptyLabel || '—')) : rows.map(row => {
    const id = row[rowKey];
    const isSel = selectedId != null && id === selectedId;
    return /*#__PURE__*/React.createElement("tr", {
      key: id,
      onClick: onRowClick ? () => onRowClick(row) : undefined,
      onMouseEnter: () => setHoverRow(id),
      onMouseLeave: () => setHoverRow(null),
      style: {
        background: isSel ? 'var(--surface-selected)' : hoverRow === id ? 'var(--surface-hover)' : 'transparent',
        cursor: onRowClick ? 'pointer' : 'default',
        transition: 'background var(--motion-instant) linear'
      }
    }, columns.map(col => /*#__PURE__*/React.createElement("td", {
      key: col.key,
      style: {
        height: 'var(--row-height)',
        textAlign: col.align || 'left',
        padding: '0 var(--table-cell-padding-x)',
        borderBottom: '1px solid var(--border-hairline)',
        font: col.mono ? 'var(--type-data)' : 'var(--type-body)',
        color: col.muted ? 'var(--text-muted)' : 'var(--text-primary)',
        whiteSpace: 'nowrap',
        verticalAlign: 'middle'
      }
    }, col.render ? col.render(row) : row[col.key])));
  })));
}
Object.assign(__ds_scope, { DataTable });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/data/DataTable.jsx", error: String((e && e.message) || e) }); }

// components/data/DataValue.jsx
try { (() => {
/* Every ID is real and inspectable: full value, mono, copyable on click.
   Never truncate a permission or hash and leave a tooltip as the only way to
   read it. */
function DataValue({
  value,
  copyable = true,
  kind = 'id',
  style
}) {
  const isPermission = kind === 'permission';
  const [copied, setCopied] = React.useState(false);
  const [hover, setHover] = React.useState(false);
  const copy = () => {
    if (!copyable) return;
    if (navigator.clipboard) navigator.clipboard.writeText(String(value)).catch(() => {});
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };
  return /*#__PURE__*/React.createElement("span", {
    onClick: copy,
    onMouseEnter: () => setHover(true),
    onMouseLeave: () => setHover(false),
    title: copyable ? 'Click to copy' : undefined,
    style: {
      display: 'inline-flex',
      alignItems: 'center',
      gap: 'var(--icon-gap)',
      font: 'var(--type-data)',
      letterSpacing: 'var(--tracking-data)',
      color: isPermission ? 'var(--corvid-violet)' : kind === 'timestamp' ? 'var(--text-muted)' : 'var(--text-primary)',
      background: isPermission ? 'var(--violet-tint)' : hover && copyable ? 'var(--surface-hover)' : 'transparent',
      padding: isPermission ? '1px 5px' : '1px 3px',
      borderRadius: 'var(--radius-hair)',
      cursor: copyable ? 'copy' : 'default',
      ...style
    }
  }, value, copyable && (hover || copied) ? /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: copied ? 'check' : 'copy',
    size: 12,
    style: {
      color: copied ? 'var(--status-ok)' : 'var(--text-muted)'
    }
  }) : null);
}
Object.assign(__ds_scope, { DataValue });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/data/DataValue.jsx", error: String((e && e.message) || e) }); }

// components/data/StatusBadge.jsx
try { (() => {
const STATUS = {
  ok: 'var(--status-ok)',
  caution: 'var(--status-caution)',
  risk: 'var(--status-risk)',
  neutral: 'var(--status-neutral)'
};

/* Text only. The word IS the badge — colour is carried by the type, not by a
   filled capsule. Functional palette; never decorative. */
function StatusBadge({
  status = 'neutral',
  children,
  style
}) {
  return /*#__PURE__*/React.createElement("span", {
    style: {
      font: 'var(--weight-medium) var(--size-body)/var(--leading-body) var(--font-sans)',
      color: STATUS[status] || STATUS.neutral,
      whiteSpace: 'nowrap',
      ...style
    }
  }, children);
}
Object.assign(__ds_scope, { StatusBadge });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/data/StatusBadge.jsx", error: String((e && e.message) || e) }); }

// components/data/PermissionList.jsx
try { (() => {
/* Permissions are revoked ONE AT A TIME. Revoking everything an agent holds is
   a separate, rarer act — bundling them into a single "revoke access" button
   made the common case (drop the one permission that's too wide) impossible. */
function PermissionList({
  permissions = [],
  onRevoke,
  onRevokeAll,
  busyId,
  style
}) {
  const [hoverId, setHoverId] = React.useState(null);
  return /*#__PURE__*/React.createElement("div", {
    style: style
  }, permissions.map(p => {
    const revoked = p.state === 'Revoked';
    return /*#__PURE__*/React.createElement("div", {
      key: p.id,
      onMouseEnter: () => setHoverId(p.id),
      onMouseLeave: () => setHoverId(null),
      style: {
        display: 'flex',
        alignItems: 'center',
        gap: 'var(--space-2)',
        minHeight: 38,
        padding: '6px 0',
        borderBottom: '1px solid var(--border-hairline)',
        opacity: revoked ? 0.5 : 1
      }
    }, /*#__PURE__*/React.createElement("span", {
      style: {
        flex: 1,
        minWidth: 0,
        display: 'flex',
        flexDirection: 'column',
        gap: 2
      }
    }, /*#__PURE__*/React.createElement("span", {
      style: {
        display: 'flex',
        alignItems: 'center',
        gap: 6
      }
    }, /*#__PURE__*/React.createElement(__ds_scope.DataValue, {
      kind: "permission",
      value: p.permission,
      copyable: false
    }), /*#__PURE__*/React.createElement("span", {
      style: {
        font: 'var(--type-caption)',
        color: 'var(--text-muted)'
      }
    }, "on"), /*#__PURE__*/React.createElement("span", {
      style: {
        font: 'var(--type-body)',
        textDecoration: revoked ? 'line-through' : 'none'
      }
    }, p.service)), /*#__PURE__*/React.createElement("span", {
      style: {
        display: 'flex',
        alignItems: 'center',
        gap: 8
      }
    }, /*#__PURE__*/React.createElement(__ds_scope.StatusBadge, {
      status: p.s
    }, p.state), /*#__PURE__*/React.createElement("span", {
      style: {
        font: 'var(--type-caption)',
        color: 'var(--text-muted)'
      }
    }, revoked ? 'revoked ' + p.revokedAt : p.expires === 'never' ? 'no expiry' : 'expires ' + p.expires))), revoked ? null : /*#__PURE__*/React.createElement("button", {
      type: "button",
      onClick: () => onRevoke && onRevoke(p),
      disabled: busyId === p.id,
      style: {
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        height: 'var(--control-height-sm)',
        padding: '0 8px',
        flex: '0 0 auto',
        background: 'transparent',
        border: '1px solid ' + (hoverId === p.id ? 'var(--status-risk)' : 'var(--border-hairline)'),
        color: hoverId === p.id ? 'var(--status-risk)' : 'var(--text-secondary)',
        borderRadius: 'var(--radius)',
        cursor: 'pointer',
        font: 'var(--weight-medium) var(--size-caption)/1 var(--font-sans)',
        transition: 'color var(--motion-fast) var(--ease-out), border-color var(--motion-fast) var(--ease-out)'
      }
    }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
      name: "x",
      size: 12
    }), "Revoke"));
  }), permissions.length === 0 ? /*#__PURE__*/React.createElement("span", {
    style: {
      font: 'var(--type-body)',
      color: 'var(--text-muted)'
    }
  }, "This agent holds no permissions.") : null, onRevokeAll && permissions.some(p => p.state !== 'Revoked') ? /*#__PURE__*/React.createElement("button", {
    type: "button",
    onClick: onRevokeAll,
    style: {
      marginTop: 'var(--space-2)',
      background: 'transparent',
      border: 'none',
      padding: 0,
      color: 'var(--status-risk)',
      cursor: 'pointer',
      font: 'var(--type-caption)'
    }
  }, "Revoke all ", permissions.filter(p => p.state !== 'Revoked').length, " permissions") : null);
}
Object.assign(__ds_scope, { PermissionList });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/data/PermissionList.jsx", error: String((e && e.message) || e) }); }

// components/data/TimelineRow.jsx
try { (() => {
/* Audit/event row: mono timestamp, plain-language description, actor initial,
   expandable. The expanded area shows a rendered explanation first; the raw
   payload is available underneath but is not the primary view — JSON is what
   the event was, not what it means. */
function TimelineRow({
  timestamp,
  actor,
  children,
  detail,
  payload,
  status,
  defaultOpen = false,
  style
}) {
  const [open, setOpen] = React.useState(defaultOpen);
  const [raw, setRaw] = React.useState(false);
  const [hover, setHover] = React.useState(false);
  const expandable = !!(detail || payload);
  return /*#__PURE__*/React.createElement("div", {
    style: {
      borderBottom: '1px solid var(--border-hairline)',
      ...style
    }
  }, /*#__PURE__*/React.createElement("div", {
    onClick: expandable ? () => setOpen(o => !o) : undefined,
    onMouseEnter: () => setHover(true),
    onMouseLeave: () => setHover(false),
    style: {
      display: 'grid',
      gridTemplateColumns: '170px 24px 1fr 16px',
      alignItems: 'center',
      gap: 'var(--space-2)',
      minHeight: 'var(--row-height)',
      padding: '6px var(--table-cell-padding-x)',
      background: hover && expandable ? 'var(--surface-hover)' : 'transparent',
      cursor: expandable ? 'pointer' : 'default'
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      font: 'var(--type-data)',
      color: 'var(--text-muted)',
      letterSpacing: 'var(--tracking-data)'
    }
  }, timestamp), /*#__PURE__*/React.createElement("span", {
    title: actor,
    style: {
      width: 20,
      height: 20,
      borderRadius: 'var(--radius-hair)',
      background: status === 'risk' ? 'var(--status-risk-tint)' : 'var(--violet-tint)',
      color: status === 'risk' ? 'var(--status-risk)' : 'var(--corvid-violet)',
      font: 'var(--weight-semibold) 11px/20px var(--font-sans)',
      textAlign: 'center'
    }
  }, (actor || '?').replace(/[^A-Za-z0-9]/g, '').slice(0, 1).toUpperCase()), /*#__PURE__*/React.createElement("span", {
    style: {
      font: 'var(--type-body)',
      color: 'var(--text-primary)'
    }
  }, children), expandable ? /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: open ? 'chevron-down' : 'chevron-right',
    size: 14,
    style: {
      color: 'var(--text-muted)'
    }
  }) : /*#__PURE__*/React.createElement("span", null)), open && expandable ? /*#__PURE__*/React.createElement("div", {
    style: {
      padding: 'var(--space-3) var(--space-3) var(--space-3) 190px',
      background: 'var(--surface-panel)'
    }
  }, detail, payload ? /*#__PURE__*/React.createElement("div", {
    style: {
      marginTop: detail ? 'var(--space-3)' : 0
    }
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    onClick: e => {
      e.stopPropagation();
      setRaw(r => !r);
    },
    style: {
      display: 'inline-flex',
      alignItems: 'center',
      gap: 4,
      background: 'transparent',
      border: 'none',
      padding: 0,
      cursor: 'pointer',
      font: 'var(--type-caption)',
      color: 'var(--text-muted)'
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: raw ? 'chevron-down' : 'chevron-right',
    size: 12
  }), "Raw payload"), raw ? /*#__PURE__*/React.createElement("pre", {
    style: {
      margin: '6px 0 0',
      padding: 'var(--space-2)',
      font: 'var(--type-data)',
      color: 'var(--text-secondary)',
      background: 'var(--surface-sunken)',
      borderRadius: 'var(--radius-hair)',
      whiteSpace: 'pre-wrap',
      overflowX: 'auto'
    }
  }, typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2)) : null) : null) : null);
}
Object.assign(__ds_scope, { TimelineRow });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/data/TimelineRow.jsx", error: String((e && e.message) || e) }); }

// components/graph/AgentGraph.jsx
try { (() => {
const NODE_W = 200;
const NODE_H = 46;
const COL_GAP = 128;
const ROW_GAP = 12;
const LABEL_H = 22;
const PAD = 28;
const STATUS_FG = {
  ok: 'var(--status-ok)',
  caution: 'var(--status-caution)',
  risk: 'var(--status-risk)',
  neutral: 'var(--status-neutral)'
};
const STATUS_BG = {
  ok: 'var(--status-ok-tint)',
  caution: 'var(--status-caution-tint)',
  risk: 'var(--status-risk-tint)',
  neutral: 'var(--status-neutral-tint)'
};
const COLUMNS = ['root', 'agent', 'resource'];
const COLUMN_LABELS = {
  root: 'Root',
  agent: 'Agents',
  resource: 'Services'
};

/* Three fixed columns — root, agents, services — because that is the shape of
   what can be known statically: an org grants an agent permission on a service.
   Agent-to-agent delegation happens at runtime and is not drawn here; inferring
   a static delegation tree would be inventing structure the data doesn't have. */
function layout(nodes, edges) {
  const cols = COLUMNS.map(kind => nodes.filter(n => (n.kind || 'agent') === kind));
  const colHeight = l => l.length ? l.length * NODE_H + (l.length - 1) * ROW_GAP : 0;
  const tallest = Math.max(...cols.map(colHeight), 0);

  /* order services by the mean row of the agents holding permission on them,
     so the connecting curves stay mostly untangled */
  const agentRow = {};
  cols[1].forEach((n, i) => {
    agentRow[n.id] = i;
  });
  const holders = {};
  edges.forEach(e => {
    (holders[e.to] = holders[e.to] || []).push(e.from);
  });
  cols[2] = [...cols[2]].sort((a, b) => {
    const mean = n => {
      const rs = (holders[n.id] || []).map(p => agentRow[p]).filter(r => r != null);
      return rs.length ? rs.reduce((s, r) => s + r, 0) / rs.length : 99;
    };
    return mean(a) - mean(b);
  });
  const pos = {};
  const present = [];
  cols.forEach((col, k) => {
    if (!col.length) return;
    present.push(COLUMNS[k]);
    const x = PAD + present.length - 1;
    let y = PAD + LABEL_H + (tallest - colHeight(col)) / 2;
    col.forEach(n => {
      pos[n.id] = {
        x: 0,
        y,
        w: NODE_W,
        kind: COLUMNS[k]
      };
      y += NODE_H + ROW_GAP;
    });
  });
  present.forEach((kind, i) => {
    const x = PAD + i * (NODE_W + COL_GAP);
    Object.values(pos).forEach(p => {
      if (p.kind === kind) p.x = x;
    });
  });
  return {
    pos,
    columns: present,
    width: PAD * 2 + present.length * NODE_W + Math.max(0, present.length - 1) * COL_GAP,
    height: PAD * 2 + LABEL_H + tallest
  };
}
function NodeCard({
  node,
  at,
  selected,
  dimmed,
  faded,
  onSelect
}) {
  const [hover, setHover] = React.useState(false);
  const kind = node.kind || 'agent';
  const isResource = kind === 'resource';
  const isRoot = kind === 'root';
  const fg = STATUS_FG[node.status] || 'var(--text-muted)';
  const bg = STATUS_BG[node.status] || 'var(--surface-sunken)';
  const subtitle = isResource ? node.system : node.permissions;
  /* a root holding a wildcard is definitional; only a wildcard GRANTED to an
     agent is a finding */
  const wildcard = !isResource && !isRoot && subtitle && subtitle.includes('*');
  return /*#__PURE__*/React.createElement("div", {
    onPointerDown: e => e.stopPropagation(),
    onClick: e => {
      e.stopPropagation();
      onSelect && onSelect(node);
    },
    onMouseEnter: () => setHover(true),
    onMouseLeave: () => setHover(false),
    style: {
      position: 'absolute',
      left: at.x,
      top: at.y,
      width: at.w,
      height: NODE_H,
      display: 'flex',
      alignItems: 'center',
      gap: 10,
      padding: '0 10px',
      background: selected ? 'var(--surface-selected)' : 'var(--surface-overlay)',
      border: '1px solid ' + (selected ? 'var(--corvid-violet)' : hover ? 'var(--border-strong)' : 'var(--border-hairline)'),
      borderRadius: 'var(--radius)',
      boxShadow: selected || hover ? '0 1px 3px rgba(21,23,28,.07)' : 'none',
      opacity: faded ? 0.18 : dimmed ? 0.4 : 1,
      cursor: 'pointer',
      boxSizing: 'border-box',
      transition: 'opacity var(--motion-fast) var(--ease-out), border-color var(--motion-fast) var(--ease-out), box-shadow var(--motion-fast) var(--ease-out)'
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      flex: '0 0 auto',
      width: 22,
      height: 22,
      borderRadius: 'var(--radius-hair)',
      background: isResource ? 'var(--surface-sunken)' : bg,
      color: isResource ? 'var(--text-secondary)' : fg
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: node.icon || (isResource ? 'database' : isRoot ? 'shield' : 'bot'),
    size: 13
  })), /*#__PURE__*/React.createElement("span", {
    style: {
      minWidth: 0,
      display: 'flex',
      flexDirection: 'column',
      gap: 1
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      font: 'var(--type-body-medium)',
      letterSpacing: 'var(--tracking-normal)',
      overflow: 'hidden',
      textOverflow: 'ellipsis',
      whiteSpace: 'nowrap'
    }
  }, node.label), subtitle ? /*#__PURE__*/React.createElement("span", {
    style: {
      display: 'flex',
      alignItems: 'center',
      gap: 4,
      font: isResource ? 'var(--type-caption)' : 'var(--type-data)',
      fontSize: 11,
      color: wildcard ? 'var(--status-risk)' : 'var(--text-muted)',
      overflow: 'hidden',
      whiteSpace: 'nowrap'
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      overflow: 'hidden',
      textOverflow: 'ellipsis'
    }
  }, subtitle), wildcard ? /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "shield-alert",
    size: 11,
    style: {
      flex: '0 0 auto'
    }
  }) : null) : null), !isResource && (node.status === 'risk' || node.status === 'caution') ? /*#__PURE__*/React.createElement("span", {
    style: {
      marginLeft: 'auto',
      flex: '0 0 auto',
      width: 5,
      height: 5,
      borderRadius: '50%',
      background: fg
    }
  }) : null);
}

/* Agent graph: who holds what, on which service. */
function AgentGraph({
  nodes = [],
  edges = [],
  selectedId,
  onSelectNode,
  showColumnLabels = true,
  query = '',
  height = '100%',
  style
}) {
  const {
    pos,
    columns,
    width,
    height: canvasH
  } = React.useMemo(() => layout(nodes, edges), [nodes, edges]);
  const [pan, setPan] = React.useState({
    x: 0,
    y: 0
  });
  const [dragging, setDragging] = React.useState(false);
  const [fit, setFit] = React.useState(1);
  const shellRef = React.useRef(null);
  const drag = React.useRef(null);
  React.useEffect(() => {
    const el = shellRef.current;
    if (!el || !window.ResizeObserver) return;
    const measure = () => {
      const {
        width: aw,
        height: ah
      } = el.getBoundingClientRect();
      if (!aw || !ah) return;
      setFit(Math.min(1, (aw - 16) / width, (ah - 16) / Math.max(canvasH, 1)));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [width, canvasH]);

  /* Drag starts only on the canvas itself — cards stop pointerdown, so a click
     on a node is never swallowed by a pan. */
  const onPointerDown = e => {
    if (e.button !== 0) return;
    drag.current = {
      sx: e.clientX,
      sy: e.clientY,
      ox: pan.x,
      oy: pan.y
    };
    setDragging(true);
  };
  const onPointerMove = e => {
    if (!drag.current) return;
    setPan({
      x: drag.current.ox + (e.clientX - drag.current.sx),
      y: drag.current.oy + (e.clientY - drag.current.sy)
    });
  };
  const endDrag = () => {
    drag.current = null;
    setDragging(false);
  };
  const related = React.useMemo(() => {
    if (!selectedId) return null;
    const set = new Set([selectedId]);
    edges.forEach(e => {
      if (e.from === selectedId) set.add(e.to);
      if (e.to === selectedId) set.add(e.from);
    });
    return set;
  }, [selectedId, edges]);
  const matches = React.useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return null;
    const set = new Set();
    nodes.forEach(n => {
      if ([n.label, n.permissions, n.system].filter(Boolean).join(' ').toLowerCase().includes(q)) set.add(n.id);
    });
    return set;
  }, [query, nodes]);
  return /*#__PURE__*/React.createElement("div", {
    ref: shellRef,
    onPointerDown: onPointerDown,
    onPointerMove: onPointerMove,
    onPointerUp: endDrag,
    onPointerLeave: endDrag,
    onPointerCancel: endDrag,
    style: {
      position: 'relative',
      width: '100%',
      height,
      overflow: 'hidden',
      background: 'var(--graph-canvas)',
      cursor: dragging ? 'grabbing' : 'grab',
      touchAction: 'none',
      ...style
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      position: 'relative',
      width,
      height: Math.max(canvasH, 100),
      transform: `translate(${pan.x}px,${pan.y}px) scale(${fit})`,
      transformOrigin: '0 0'
    }
  }, /*#__PURE__*/React.createElement("svg", {
    width: width,
    height: Math.max(canvasH, 100),
    style: {
      position: 'absolute',
      inset: 0,
      display: 'block',
      pointerEvents: 'none'
    }
  }, edges.map((e, i) => {
    const a = pos[e.from];
    const b = pos[e.to];
    if (!a || !b) return null;
    const stroke = e.kind === 'inactive' ? 'var(--graph-edge-inactive)' : 'var(--graph-edge-grant)';
    const active = related && related.has(e.from) && related.has(e.to);
    const dimmed = related && !active || matches && !(matches.has(e.from) && matches.has(e.to));
    const x1 = a.x + a.w;
    const y1 = a.y + NODE_H / 2;
    const x2 = b.x;
    const y2 = b.y + NODE_H / 2;
    const c = Math.max(40, (x2 - x1) * 0.5);
    const mx = (x1 + x2) / 2;
    const my = (y1 + y2) / 2 - 5;
    const label = e.label;
    return /*#__PURE__*/React.createElement("g", {
      key: i,
      opacity: dimmed ? 0.1 : active ? 1 : 0.45,
      style: {
        transition: 'opacity var(--motion-fast) var(--ease-out)'
      }
    }, /*#__PURE__*/React.createElement("path", {
      d: `M${x1},${y1} C${x1 + c},${y1} ${x2 - c},${y2} ${x2},${y2}`,
      fill: "none",
      stroke: stroke,
      strokeWidth: e.width || 1.25,
      strokeDasharray: e.kind === 'inactive' ? '3 4' : undefined,
      strokeLinecap: "round"
    }), active && label ? /*#__PURE__*/React.createElement("g", null, /*#__PURE__*/React.createElement("rect", {
      x: mx - (label.length * 6.4 + 8) / 2,
      y: my - 8,
      width: label.length * 6.4 + 8,
      height: 16,
      fill: "var(--graph-canvas)"
    }), /*#__PURE__*/React.createElement("text", {
      x: mx,
      y: my + 4,
      textAnchor: "middle",
      style: {
        font: 'var(--type-data)',
        fontSize: 11,
        fill: 'var(--text-muted)'
      }
    }, label)) : null);
  })), showColumnLabels ? columns.map((kind, k) => /*#__PURE__*/React.createElement("div", {
    key: kind,
    style: {
      position: 'absolute',
      left: PAD + k * (NODE_W + COL_GAP),
      top: PAD - 4,
      width: NODE_W,
      font: 'var(--type-caption)',
      color: 'var(--text-muted)'
    }
  }, COLUMN_LABELS[kind])) : null, nodes.map(n => pos[n.id] ? /*#__PURE__*/React.createElement(NodeCard, {
    key: n.id,
    node: n,
    at: pos[n.id],
    selected: n.id === selectedId,
    dimmed: related ? !related.has(n.id) : false,
    faded: matches ? !matches.has(n.id) : false,
    onSelect: onSelectNode
  }) : null)));
}
Object.assign(__ds_scope, { AgentGraph });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/graph/AgentGraph.jsx", error: String((e && e.message) || e) }); }

// components/graph/DelegationTimeline.jsx
try { (() => {
const ROW_H = 38;
const LABEL_W = 300;
const BAR_TOP = 10;
const BAR_H = 18;
const parse = t => t && t !== 'never' && t !== 'expired' ? Date.parse(t.replace(' ', 'T')) : null;
const DEPTH_COLOR = [{
  bar: 'var(--corvid-violet)',
  tint: 'var(--violet-tint)'
}, {
  bar: 'var(--slate-teal)',
  tint: 'var(--teal-tint)'
}, {
  bar: 'var(--status-caution)',
  tint: 'var(--status-caution-tint)'
}];
const colorFor = depth => DEPTH_COLOR[Math.min(depth - 1, DEPTH_COLOR.length - 1)];

/* Chain order: each originating delegation, then everything descended from it,
   depth-first and by start time. Chronological order scatters a chain across the
   view; chain order makes each one a contiguous block you can read top to bottom. */
function orderRows(delegations, mode) {
  const byStart = (a, b) => (parse(a.started) || 0) - (parse(b.started) || 0);
  if (mode === 'time') return [...delegations].sort(byStart);
  const children = {};
  delegations.forEach(d => {
    if (d.parent) (children[d.parent] = children[d.parent] || []).push(d);
  });
  const out = [];
  const walk = d => {
    out.push(d);
    (children[d.id] || []).sort(byStart).forEach(walk);
  };
  delegations.filter(d => !d.parent || !delegations.some(x => x.id === d.parent)).sort(byStart).forEach(walk);
  delegations.forEach(d => {
    if (!out.includes(d)) out.push(d);
  });
  return out;
}

/* Runtime delegation trace. Bars answer WHEN; the row labels and the connectors
   between them answer WHO PASSED WHAT TO WHOM — a connector drops from the
   parent's bar to the child's at the x-position of the moment the hop happened,
   so the act of delegating is visible rather than implied by indentation. */
function DelegationTimeline({
  delegations = [],
  windowStart,
  windowEnd,
  now,
  order = 'chain',
  selectedId,
  onSelect,
  height = '100%',
  style
}) {
  const t0 = parse(windowStart);
  const t1 = parse(windowEnd);
  const tNow = parse(now) || t0;
  const span = Math.max(1, t1 - t0);
  const pct = t => (t - t0) / span * 100;
  const rows = React.useMemo(() => orderRows(delegations, order), [delegations, order]);
  const rowIndex = React.useMemo(() => Object.fromEntries(rows.map((d, i) => [d.id, i])), [rows]);
  const ticks = React.useMemo(() => {
    const out = [];
    const step = 3600 * 1000 * (span > 12 * 3600 * 1000 ? 3 : 1);
    let t = Math.ceil(t0 / step) * step;
    while (t <= t1) {
      out.push(t);
      t += step;
    }
    return out;
  }, [t0, t1, span]);
  const clock = ts => new Date(ts).toISOString().slice(11, 16);
  const bodyH = rows.length * ROW_H;

  /* parent → child links, drawn at the instant the child was created */
  const links = rows.filter(d => d.parent != null && rowIndex[d.parent] != null).map(d => ({
    id: d.id,
    x: pct(Math.max(parse(d.started), t0)),
    from: rowIndex[d.parent],
    to: rowIndex[d.id],
    depth: d.depth,
    active: selectedId === d.id || selectedId === d.parent
  }));
  return /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      flexDirection: 'column',
      height,
      minHeight: 0,
      background: 'var(--surface-overlay)',
      ...style
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      flex: '0 0 auto',
      borderBottom: '1px solid var(--border-hairline)'
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      width: LABEL_W,
      flex: '0 0 auto',
      padding: '6px var(--space-3)',
      font: 'var(--type-caption)',
      color: 'var(--text-muted)'
    }
  }, "Who passed it to whom"), /*#__PURE__*/React.createElement("div", {
    style: {
      position: 'relative',
      flex: 1,
      height: 26
    }
  }, ticks.map(t => /*#__PURE__*/React.createElement("span", {
    key: t,
    style: {
      position: 'absolute',
      left: pct(t) + '%',
      top: 6,
      transform: 'translateX(-50%)',
      font: 'var(--type-data)',
      fontSize: 11,
      color: 'var(--text-muted)',
      whiteSpace: 'nowrap'
    }
  }, clock(t))))), /*#__PURE__*/React.createElement("div", {
    style: {
      flex: 1,
      overflowY: 'auto'
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      minHeight: '100%'
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      width: LABEL_W,
      flex: '0 0 auto'
    }
  }, rows.map(d => {
    const sel = d.id === selectedId;
    const c = colorFor(d.depth);
    const indent = (d.depth - 1) * 16;
    return /*#__PURE__*/React.createElement("div", {
      key: d.id,
      onClick: () => onSelect && onSelect(d),
      style: {
        display: 'flex',
        alignItems: 'center',
        gap: 5,
        height: ROW_H,
        paddingRight: 'var(--space-3)',
        paddingLeft: 'calc(var(--space-3) + ' + indent + 'px)',
        background: sel ? 'var(--surface-selected)' : 'transparent',
        borderBottom: '1px solid var(--border-hairline)',
        cursor: 'pointer',
        minWidth: 0
      }
    }, /*#__PURE__*/React.createElement("span", {
      style: {
        font: 'var(--type-caption)',
        color: 'var(--text-muted)',
        maxWidth: 108,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
        flex: '0 0 auto'
      }
    }, d.from), /*#__PURE__*/React.createElement(__ds_scope.Icon, {
      name: "arrow-right",
      size: 11,
      style: {
        color: c.bar,
        flex: '0 0 auto'
      }
    }), /*#__PURE__*/React.createElement("span", {
      style: {
        font: 'var(--type-body)',
        minWidth: 0,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap'
      }
    }, d.to), /*#__PURE__*/React.createElement("span", {
      style: {
        marginLeft: 'auto',
        flex: '0 0 auto',
        font: 'var(--weight-medium) 10px var(--font-sans)',
        color: c.bar,
        background: c.tint,
        padding: '2px 4px',
        borderRadius: 'var(--radius-hair)'
      }
    }, "h", d.depth));
  })), /*#__PURE__*/React.createElement("div", {
    style: {
      position: 'relative',
      flex: 1,
      borderLeft: '1px solid var(--border-hairline)',
      minHeight: bodyH
    }
  }, ticks.map(t => /*#__PURE__*/React.createElement("span", {
    key: t,
    style: {
      position: 'absolute',
      left: pct(t) + '%',
      top: 0,
      height: bodyH,
      width: 1,
      background: 'var(--border-hairline)',
      opacity: 0.6
    }
  })), rows.map((_, i) => /*#__PURE__*/React.createElement("span", {
    key: i,
    style: {
      position: 'absolute',
      top: (i + 1) * ROW_H - 1,
      left: 0,
      right: 0,
      height: 1,
      background: 'var(--border-hairline)'
    }
  })), /*#__PURE__*/React.createElement("svg", {
    width: "100%",
    height: bodyH,
    style: {
      position: 'absolute',
      top: 0,
      left: 0,
      pointerEvents: 'none',
      overflow: 'visible'
    }
  }, links.map(l => {
    const c = colorFor(l.depth);
    const y1 = l.from * ROW_H + BAR_TOP + BAR_H;
    const y2 = l.to * ROW_H + BAR_TOP + BAR_H / 2;
    return /*#__PURE__*/React.createElement("g", {
      key: l.id,
      opacity: selectedId && !l.active ? 0.2 : 1
    }, /*#__PURE__*/React.createElement("line", {
      x1: l.x + '%',
      y1: y1,
      x2: l.x + '%',
      y2: y2,
      stroke: c.bar,
      strokeWidth: "1",
      strokeDasharray: "2 2"
    }), /*#__PURE__*/React.createElement("circle", {
      cx: l.x + '%',
      cy: y1,
      r: "2",
      fill: c.bar
    }), /*#__PURE__*/React.createElement("circle", {
      cx: l.x + '%',
      cy: y2,
      r: "2.5",
      fill: "var(--surface-overlay)",
      stroke: c.bar,
      strokeWidth: "1.5"
    }));
  })), rows.map((d, i) => {
    const start = parse(d.started);
    const end = parse(d.expires);
    const standing = d.expires === 'never';
    const retracted = d.status === 'retracted';
    const c = colorFor(d.depth);
    const left = pct(Math.max(start, t0));
    const right = standing ? 100 : pct(Math.min(end, t1));
    const sel = d.id === selectedId;
    const dim = selectedId && !sel && d.parent !== selectedId && d.id !== (rows.find(x => x.id === selectedId) || {}).parent;
    return /*#__PURE__*/React.createElement("div", {
      key: d.id,
      onClick: () => onSelect && onSelect(d),
      title: d.from + ' → ' + d.to + ' · ' + d.permission + ' on ' + d.service,
      style: {
        position: 'absolute',
        top: i * ROW_H + BAR_TOP,
        height: BAR_H,
        left: left + '%',
        width: Math.max(1, right - left) + '%',
        display: 'flex',
        alignItems: 'center',
        gap: 5,
        padding: '0 6px',
        background: retracted ? 'var(--surface-sunken)' : c.tint,
        borderLeft: '2px solid ' + (retracted ? 'var(--status-neutral)' : c.bar),
        borderRadius: 'var(--radius-hair)',
        outline: sel ? '1px solid var(--corvid-violet)' : 'none',
        opacity: retracted ? 0.55 : dim ? 0.35 : 1,
        cursor: 'pointer',
        overflow: 'hidden',
        boxSizing: 'border-box',
        transition: 'opacity var(--motion-fast) var(--ease-out)'
      }
    }, /*#__PURE__*/React.createElement("span", {
      style: {
        font: 'var(--type-data)',
        fontSize: 11,
        whiteSpace: 'nowrap',
        color: retracted ? 'var(--text-muted)' : c.bar,
        textDecoration: retracted ? 'line-through' : 'none'
      }
    }, d.permission), standing ? /*#__PURE__*/React.createElement("span", {
      style: {
        marginLeft: 'auto',
        display: 'inline-flex',
        alignItems: 'center',
        gap: 3,
        font: 'var(--type-caption)',
        color: 'var(--status-caution)',
        whiteSpace: 'nowrap'
      }
    }, "no expiry") : null);
  }), /*#__PURE__*/React.createElement("div", {
    style: {
      position: 'absolute',
      left: pct(tNow) + '%',
      top: 0,
      height: bodyH,
      width: 1,
      background: 'var(--corvid-violet)'
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      position: 'absolute',
      top: 0,
      left: 3,
      font: 'var(--type-caption)',
      color: 'var(--corvid-violet)',
      whiteSpace: 'nowrap'
    }
  }, "now"))))));
}
Object.assign(__ds_scope, { DelegationTimeline });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/graph/DelegationTimeline.jsx", error: String((e && e.message) || e) }); }

// components/shell/ConfirmModal.jsx
try { (() => {
/* Modals are reserved for destructive confirmations only — one consistent
   meaning: "a modal appeared, something is about to change." */
function ConfirmModal({
  open,
  title,
  children,
  confirmLabel = 'Revoke access',
  cancelLabel = 'Cancel',
  onConfirm,
  onCancel,
  consequence
}) {
  if (!open) return null;
  return /*#__PURE__*/React.createElement("div", {
    style: {
      position: 'fixed',
      inset: 0,
      zIndex: 60,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      background: 'var(--surface-scrim)'
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      width: 440,
      background: 'var(--surface-overlay)',
      border: '1px solid var(--border-hairline)',
      borderRadius: 'var(--radius)',
      boxShadow: 'var(--shadow-overlay)'
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      padding: 'var(--space-3)'
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      font: 'var(--type-section)'
    }
  }, title), /*#__PURE__*/React.createElement("div", {
    className: "prose",
    style: {
      marginTop: 'var(--space-2)',
      font: 'var(--type-body)',
      color: 'var(--text-secondary)'
    }
  }, children), consequence ? /*#__PURE__*/React.createElement("div", {
    style: {
      marginTop: 'var(--space-3)',
      padding: 'var(--space-2)',
      background: 'var(--status-risk-tint)',
      color: 'var(--status-risk)',
      borderRadius: 'var(--radius-hair)',
      font: 'var(--type-body)'
    }
  }, consequence) : null), /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      justifyContent: 'flex-end',
      gap: 'var(--space-2)',
      padding: 'var(--space-3)',
      borderTop: '1px solid var(--border-hairline)',
      background: 'var(--surface-panel)'
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Button, {
    variant: "ghost",
    onClick: onCancel
  }, cancelLabel), /*#__PURE__*/React.createElement(__ds_scope.Button, {
    variant: "destructive",
    onClick: onConfirm
  }, confirmLabel))));
}
Object.assign(__ds_scope, { ConfirmModal });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/shell/ConfirmModal.jsx", error: String((e && e.message) || e) }); }

// components/shell/Drawer.jsx
try { (() => {
/* 480px right drawer — the universal detail pattern. Slides in over 200ms,
   pushes nothing, no scrim (the table behind stays readable). */
function Drawer({
  open,
  title,
  subtitle,
  onClose,
  footer,
  children,
  width = 'var(--drawer-width)',
  style
}) {
  return /*#__PURE__*/React.createElement("aside", {
    "aria-hidden": !open,
    style: {
      position: 'absolute',
      top: 0,
      right: 0,
      bottom: 0,
      width,
      display: 'flex',
      flexDirection: 'column',
      background: 'var(--surface-overlay)',
      borderLeft: '1px solid var(--border-hairline)',
      boxShadow: 'var(--shadow-drawer)',
      transform: open ? 'translateX(0)' : 'translateX(100%)',
      transition: 'transform var(--motion-drawer) var(--ease-out)',
      pointerEvents: open ? 'auto' : 'none',
      zIndex: 30,
      overflow: 'hidden',
      ...style
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      alignItems: 'flex-start',
      gap: 'var(--space-2)',
      padding: 'var(--space-3)',
      borderBottom: '1px solid var(--border-hairline)'
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      flex: 1,
      minWidth: 0
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      font: 'var(--type-title)'
    }
  }, title), subtitle ? /*#__PURE__*/React.createElement("div", {
    style: {
      marginTop: 2
    }
  }, subtitle) : null), /*#__PURE__*/React.createElement("button", {
    type: "button",
    onClick: onClose,
    "aria-label": "Close",
    style: {
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      width: 24,
      height: 24,
      background: 'transparent',
      border: 'none',
      borderRadius: 'var(--radius)',
      color: 'var(--text-muted)',
      cursor: 'pointer'
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "x",
    size: 16
  }))), /*#__PURE__*/React.createElement("div", {
    style: {
      flex: 1,
      overflowY: 'auto',
      padding: 'var(--space-3)'
    }
  }, children), footer ? /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      gap: 'var(--space-2)',
      padding: 'var(--space-3)',
      borderTop: '1px solid var(--border-hairline)',
      background: 'var(--surface-panel)'
    }
  }, footer) : null);
}
Object.assign(__ds_scope, { Drawer });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/shell/Drawer.jsx", error: String((e && e.message) || e) }); }

// components/shell/EmptyState.jsx
try { (() => {
/* Explains what's missing and gives exactly one direct action.
   No illustration, no "No data yet." */
function EmptyState({
  title,
  children,
  action,
  align = 'left',
  style
}) {
  return /*#__PURE__*/React.createElement("div", {
    style: {
      padding: 'var(--space-6) var(--space-4)',
      textAlign: align,
      ...style
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      font: 'var(--type-display)',
      maxWidth: '32ch',
      margin: align === 'center' ? '0 auto' : 0
    }
  }, title), children ? /*#__PURE__*/React.createElement("p", {
    className: "prose",
    style: {
      marginTop: 'var(--space-2)',
      color: 'var(--text-secondary)',
      margin: align === 'center' ? 'var(--space-2) auto 0' : 'var(--space-2) 0 0'
    }
  }, children) : null, action ? /*#__PURE__*/React.createElement("div", {
    style: {
      marginTop: 'var(--space-4)',
      display: 'flex',
      gap: 'var(--space-2)',
      justifyContent: align === 'center' ? 'center' : 'flex-start'
    }
  }, action) : null);
}
Object.assign(__ds_scope, { EmptyState });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/shell/EmptyState.jsx", error: String((e && e.message) || e) }); }

// components/shell/NavRail.jsx
try { (() => {
function NavItem({
  item,
  active,
  onSelect
}) {
  const [hover, setHover] = React.useState(false);
  return /*#__PURE__*/React.createElement("button", {
    type: "button",
    onClick: () => onSelect && onSelect(item.id),
    onMouseEnter: () => setHover(true),
    onMouseLeave: () => setHover(false),
    style: {
      display: 'flex',
      alignItems: 'center',
      gap: 'var(--space-2)',
      height: 28,
      width: '100%',
      padding: '0 8px',
      background: active ? 'var(--surface-selected)' : hover ? 'var(--surface-hover)' : 'transparent',
      color: active ? 'var(--corvid-violet)' : 'var(--text-secondary)',
      border: 'none',
      borderRadius: 'var(--radius)',
      cursor: 'pointer',
      font: active ? 'var(--type-body-medium)' : 'var(--type-body)',
      letterSpacing: 'var(--tracking-normal)',
      whiteSpace: 'nowrap',
      textAlign: 'left',
      transition: 'background var(--motion-instant) linear'
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: item.icon,
    size: 15,
    style: {
      color: active ? 'var(--corvid-violet)' : 'var(--text-muted)'
    }
  }), /*#__PURE__*/React.createElement("span", {
    style: {
      flex: 1,
      overflow: 'hidden',
      textOverflow: 'ellipsis'
    }
  }, item.label), item.count != null ? /*#__PURE__*/React.createElement("span", {
    style: {
      font: 'var(--type-caption)',
      color: 'var(--text-muted)'
    }
  }, item.count) : null, item.tag ? /*#__PURE__*/React.createElement("span", {
    style: {
      font: 'var(--weight-medium) 9px/1 var(--font-sans)',
      letterSpacing: 'var(--tracking-label)',
      textTransform: 'uppercase',
      color: 'var(--corvid-violet)',
      background: 'var(--violet-tint)',
      padding: '3px 4px',
      borderRadius: 'var(--radius-hair)'
    }
  }, item.tag) : null);
}

/* The console's persistent left sidebar: brand at the top, grouped nav in the
   middle, the standing action and the current identity pinned at the bottom.
   Always expanded — the collapse control hides it entirely rather than
   degrading it to an icon rail. */
function NavRail({
  brand = 'ravn',
  groups = [],
  activeId,
  onSelect,
  action,
  user,
  onToggle,
  style
}) {
  return /*#__PURE__*/React.createElement("nav", {
    style: {
      display: 'flex',
      flexDirection: 'column',
      width: 'var(--sidebar-width)',
      flex: '0 0 auto',
      height: '100%',
      background: 'var(--surface-page)',
      borderRight: '1px solid var(--border-hairline)',
      overflow: 'hidden',
      ...style
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      alignItems: 'center',
      height: 'var(--topbar-height)',
      padding: '0 12px 0 10px',
      flex: '0 0 auto'
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      flex: 1,
      font: 'var(--weight-semibold) 15px var(--font-sans)',
      letterSpacing: '-.02em',
      color: 'var(--text-primary)'
    }
  }, brand), onToggle ? /*#__PURE__*/React.createElement("button", {
    type: "button",
    onClick: onToggle,
    "aria-label": "Hide sidebar (\u2318\\\\)",
    title: "Hide sidebar",
    style: {
      display: 'flex',
      background: 'transparent',
      border: 'none',
      color: 'var(--text-muted)',
      cursor: 'pointer',
      padding: 0
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "panel-left-close",
    size: 15
  })) : null), /*#__PURE__*/React.createElement("div", {
    style: {
      flex: 1,
      overflowY: 'auto',
      padding: '0 8px'
    }
  }, groups.map((group, gi) => /*#__PURE__*/React.createElement("div", {
    key: group.label || gi,
    style: {
      marginBottom: 'var(--space-3)'
    }
  }, group.label ? /*#__PURE__*/React.createElement("div", {
    style: {
      font: 'var(--weight-medium) var(--size-caption)/1 var(--font-sans)',
      color: 'var(--text-muted)',
      padding: '0 8px',
      marginBottom: 6
    }
  }, group.label) : null, /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      flexDirection: 'column',
      gap: 1
    }
  }, group.items.map(item => /*#__PURE__*/React.createElement(NavItem, {
    key: item.id,
    item: item,
    active: item.id === activeId,
    onSelect: onSelect
  })))))), /*#__PURE__*/React.createElement("div", {
    style: {
      flex: '0 0 auto',
      padding: 8,
      display: 'flex',
      flexDirection: 'column',
      gap: 'var(--space-2)'
    }
  }, action, user ? /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      alignItems: 'center',
      gap: 'var(--space-2)',
      padding: '4px 4px 2px'
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      width: 22,
      height: 22,
      borderRadius: '50%',
      flex: '0 0 auto',
      background: 'var(--violet-tint)',
      color: 'var(--corvid-violet)',
      font: 'var(--weight-semibold) 10px/22px var(--font-sans)',
      textAlign: 'center'
    }
  }, (user.name || '?').slice(0, 1).toUpperCase()), /*#__PURE__*/React.createElement("span", {
    style: {
      minWidth: 0,
      display: 'flex',
      flexDirection: 'column'
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      font: 'var(--type-body-medium)',
      overflow: 'hidden',
      textOverflow: 'ellipsis',
      whiteSpace: 'nowrap'
    }
  }, user.name), user.org ? /*#__PURE__*/React.createElement("span", {
    style: {
      font: 'var(--type-caption)',
      color: 'var(--text-muted)',
      overflow: 'hidden',
      textOverflow: 'ellipsis',
      whiteSpace: 'nowrap'
    }
  }, user.org) : null)) : null));
}
Object.assign(__ds_scope, { NavRail });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/shell/NavRail.jsx", error: String((e && e.message) || e) }); }

// components/shell/Toast.jsx
try { (() => {
/* Slides up from bottom-right, auto-dismisses. Confirmations only. */
function Toast({
  open,
  children,
  status = 'ok',
  onDismiss,
  autoDismissMs = 4000,
  style
}) {
  React.useEffect(() => {
    if (!open || !autoDismissMs || !onDismiss) return;
    const t = window.setTimeout(onDismiss, autoDismissMs);
    return () => window.clearTimeout(t);
  }, [open, autoDismissMs, onDismiss]);
  const fg = status === 'risk' ? 'var(--status-risk)' : status === 'caution' ? 'var(--status-caution)' : 'var(--status-ok)';
  return /*#__PURE__*/React.createElement("div", {
    role: "status",
    style: {
      position: 'absolute',
      right: 'var(--space-3)',
      bottom: 'var(--space-3)',
      zIndex: 70,
      display: 'flex',
      alignItems: 'center',
      gap: 'var(--space-2)',
      maxWidth: 380,
      padding: '10px var(--space-3)',
      background: 'var(--surface-overlay)',
      border: '1px solid var(--border-hairline)',
      borderRadius: 'var(--radius)',
      boxShadow: 'var(--shadow-toast)',
      font: 'var(--type-body)',
      transform: open ? 'translateY(0)' : 'translateY(12px)',
      opacity: open ? 1 : 0,
      pointerEvents: open ? 'auto' : 'none',
      transition: 'transform var(--motion-drawer) var(--ease-out), opacity var(--motion-drawer) var(--ease-out)',
      ...style
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: status === 'ok' ? 'check' : 'alert-triangle',
    size: 16,
    style: {
      color: fg
    }
  }), /*#__PURE__*/React.createElement("span", {
    style: {
      flex: 1
    }
  }, children), onDismiss ? /*#__PURE__*/React.createElement("button", {
    type: "button",
    onClick: onDismiss,
    "aria-label": "Dismiss",
    style: {
      background: 'transparent',
      border: 'none',
      color: 'var(--text-muted)',
      cursor: 'pointer',
      display: 'flex'
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "x",
    size: 14
  })) : null);
}
Object.assign(__ds_scope, { Toast });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/shell/Toast.jsx", error: String((e && e.message) || e) }); }

// components/shell/TopBar.jsx
try { (() => {
/* Light 48px title header. Not chrome: it carries where you are (icon + view
   name, or a breadcrumb) on the left and view-level controls on the right.
   Org and environment context lives in the sidebar's identity block, not here. */
function TopBar({
  icon,
  title,
  crumbs,
  right,
  onClear,
  onShowSidebar,
  brand = 'ravn',
  style
}) {
  const [hover, setHover] = React.useState(null);
  const iconBtn = (name, label, onClick) => /*#__PURE__*/React.createElement("button", {
    key: name,
    type: "button",
    onClick: onClick,
    "aria-label": label,
    title: label,
    onMouseEnter: () => setHover(name),
    onMouseLeave: () => setHover(null),
    style: {
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      width: 24,
      height: 24,
      background: hover === name ? 'var(--surface-hover)' : 'transparent',
      border: 'none',
      borderRadius: 'var(--radius)',
      color: 'var(--text-muted)',
      cursor: 'pointer'
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: name,
    size: 15
  }));
  return /*#__PURE__*/React.createElement("header", {
    style: {
      display: 'flex',
      alignItems: 'center',
      gap: 'var(--space-2)',
      height: 'var(--topbar-height)',
      padding: '0 var(--space-3)',
      flex: '0 0 auto',
      background: 'var(--surface-overlay)',
      ...style
    }
  }, onShowSidebar ? /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      alignItems: 'center',
      gap: 2,
      marginRight: 'var(--space-2)',
      paddingRight: 'var(--space-2)',
      borderRight: '1px solid var(--border-hairline)'
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      font: 'var(--weight-semibold) 15px var(--font-sans)',
      letterSpacing: '-.02em',
      color: 'var(--text-primary)',
      marginRight: 4
    }
  }, brand), iconBtn('panel-left-open', 'Show sidebar (⌘\\)', onShowSidebar)) : null, icon ? /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: icon,
    size: 15,
    style: {
      color: 'var(--text-muted)'
    }
  }) : null, crumbs ? /*#__PURE__*/React.createElement("div", {
    style: {
      display: 'flex',
      alignItems: 'center',
      gap: 6,
      font: 'var(--type-body)'
    }
  }, crumbs.map((c, i) => /*#__PURE__*/React.createElement(React.Fragment, {
    key: c
  }, i > 0 ? /*#__PURE__*/React.createElement("span", {
    style: {
      color: 'var(--border-strong)'
    }
  }, "/") : null, /*#__PURE__*/React.createElement("span", {
    style: {
      color: i === crumbs.length - 1 ? 'var(--text-primary)' : 'var(--text-muted)',
      font: i === crumbs.length - 1 ? 'var(--type-body-medium)' : 'var(--type-body)'
    }
  }, c)))) : /*#__PURE__*/React.createElement("span", {
    style: {
      font: 'var(--type-body-medium)',
      letterSpacing: 'var(--tracking-normal)'
    }
  }, title), /*#__PURE__*/React.createElement("div", {
    style: {
      flex: 1
    }
  }), onClear ? /*#__PURE__*/React.createElement("button", {
    type: "button",
    onClick: onClear,
    style: {
      display: 'flex',
      alignItems: 'center',
      gap: 3,
      background: 'transparent',
      border: 'none',
      color: 'var(--text-muted)',
      cursor: 'pointer',
      font: 'var(--type-body)',
      padding: '0 4px'
    }
  }, /*#__PURE__*/React.createElement(__ds_scope.Icon, {
    name: "x",
    size: 13
  }), "Clear") : null, right);
}
Object.assign(__ds_scope, { TopBar });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/shell/TopBar.jsx", error: String((e && e.message) || e) }); }

if (__ds_ns.__errors.length) throw new Error("Reference component initialization failed");
export default __ds_scope;
