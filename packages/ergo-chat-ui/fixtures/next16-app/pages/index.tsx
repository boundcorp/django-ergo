import { useState } from 'react'
import {
  AppBar,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Drawer,
  List,
  ListItemButton,
  ListItemText,
  Toolbar,
  Typography,
} from '@mui/material'
import { makeStyles } from 'tss-react/mui'
import Demo from '../Demo'

const DRAWER = 220

const useStyles = makeStyles()(theme => ({
  main: {
    display: 'flex',
    flexDirection: 'column',
    height: '100vh',
    boxSizing: 'border-box',
    padding: theme.spacing(2),
    [theme.breakpoints.up('md')]: { marginLeft: DRAWER },
  },
  chat: {
    flex: 1,
    minHeight: 0,
    background: theme.palette.background.paper,
    border: `1px solid ${theme.palette.divider}`,
  },
}))

export default function Home() {
  const { classes } = useStyles()
  const [menu, setMenu] = useState(false)
  const [dialog, setDialog] = useState(false)
  return (
    <div className="host-root">
      <AppBar position="fixed" sx={{ zIndex: t => t.zIndex.drawer + 1 }}>
        <Toolbar>
          <Button color="inherit" data-testid="menu-open" onClick={() => setMenu(true)}>
            Menu
          </Button>
          <Typography variant="h6" sx={{ flex: 1 }}>
            Host app
          </Typography>
          <Button color="inherit" data-testid="dialog-open" onClick={() => setDialog(true)}>
            Settings
          </Button>
        </Toolbar>
      </AppBar>
      <Drawer
        variant="permanent"
        sx={{ display: { xs: 'none', md: 'block' }, '& .MuiDrawer-paper': { width: DRAWER } }}
      >
        <Toolbar />
        <List>
          <ListItemButton>
            <ListItemText primary="Chats" />
          </ListItemButton>
        </List>
      </Drawer>
      <Drawer open={menu} onClose={() => setMenu(false)} data-testid="temp-drawer">
        <Box sx={{ width: DRAWER, p: 2 }}>Temporary drawer</Box>
      </Drawer>
      <Dialog open={dialog} onClose={() => setDialog(false)} data-testid="dialog">
        <DialogTitle>Host settings</DialogTitle>
        <DialogContent>A modal over the chat. It must stack above it.</DialogContent>
        <DialogActions>
          <Button onClick={() => setDialog(false)}>Close</Button>
        </DialogActions>
      </Dialog>
      <main className={classes.main}>
        <Toolbar />
        <div className={classes.chat} data-testid="chat-host">
          <Demo style={{ height: '100%', fontFamily: 'inherit' }} />
        </div>
      </main>
    </div>
  )
}
