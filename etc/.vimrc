" -----------------------------------------
" Basic Setup
" -----------------------------------------
set nocompatible      " Disable compatibility with old-time vi
syntax on             " Enable syntax highlighting
filetype plugin indent on " Enable filetype-specific indenting and plugins

" -----------------------------------------
" Indentation (Strict 2 spaces)
" -----------------------------------------
set tabstop=2         " Number of visual spaces per TAB
set shiftwidth=2      " Number of spaces to use for autoindent
set expandtab         " Tabs are converted to spaces
set autoindent        " Copy indent from current line when starting a new line
set smartindent       " Smart autoindenting for programming

" -----------------------------------------
" UI & Formatting
" -----------------------------------------
set number            " Show line numbers
set cursorline        " Highlight the current line
set showmatch         " Highlight matching brackets/braces
set nowrap            " Do not wrap long lines visually
set scrolloff=5       " Keep 5 lines visible above/below the cursor when scrolling
set wildmenu          " Visual autocomplete for command menu

" -----------------------------------------
" Search
" -----------------------------------------
set hlsearch          " Highlight search results
set incsearch         " Show search matches as you type
set ignorecase        " Ignore case in search patterns
set smartcase         " Override ignorecase if search contains uppercase

" -----------------------------------------
" Server Admin Quality of Life
" -----------------------------------------
set noswapfile        " Don't create .swp files (keeps config directories clean)
set nobackup          " Don't create backup files
set nowritebackup     " Don't create backup files while editing
set pastetoggle=<F2>  " Press F2 before pasting text to prevent cascading indents
