import { createTheme } from '@mui/material/styles'

// A deliberately opinionated theme: font, palette and shape differ from the chat package's defaults, so the
// coexistence checks can tell whose styles apply where.
export const theme = createTheme({
  typography: { fontFamily: '"Roboto", "Helvetica", "Arial", sans-serif', fontSize: 14 },
  palette: { primary: { main: '#0b6e4f' }, background: { default: '#f4f6f4' } },
  shape: { borderRadius: 6 },
})
